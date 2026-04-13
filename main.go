package main

import (
	"crypto/rand"
	"embed"
	"encoding/hex"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/securecookie"
	"github.com/gorilla/websocket"
	"github.com/joho/godotenv"
	"golang.org/x/crypto/bcrypt"

	db "websocket-relay/db"
)

//go:embed static/*
var content embed.FS

var (
	upgrader = websocket.Upgrader{
		CheckOrigin: func(r *http.Request) bool { return true }, // Logic to allow all origins
	}
)

type Slave struct {
	ID   string
	Conn *websocket.Conn
}

type Relay struct {
	mu           sync.RWMutex
	slaves       map[string]*Slave
	master       *websocket.Conn
	masterTarget string
}

func NewRelay() *Relay {
	relay := &Relay{slaves: make(map[string]*Slave)}
	go relay.heartbeatTask()
	return relay
}

func (rel *Relay) HasSlave(id string) bool {
	rel.mu.RLock()
	_, exists := rel.slaves[id]
	rel.mu.RUnlock()
	return exists
}

func (rel *Relay) WithSlaves(fn func(slaves map[string]*Slave)) {
	rel.mu.RLock()
	fn(rel.slaves)
	rel.mu.RUnlock()
}

func (rel *Relay) HandleMaster(conn *websocket.Conn, slaveID string) {
	rel.mu.Lock()
	slave, exists := rel.slaves[slaveID]
	rel.mu.Unlock()
	if !exists {
		conn.Close()
		return
	}

	rel.mu.Lock()
	rel.master = conn
	rel.masterTarget = slaveID
	rel.mu.Unlock()

	slave.Conn.WriteMessage(websocket.TextMessage, []byte("CONNECTED"))

	defer func() {
		rel.mu.Lock()
		rel.master = nil
		rel.masterTarget = ""
		rel.mu.Unlock()
		slave.Conn.WriteMessage(websocket.TextMessage, []byte("DISCONNECTED"))
		conn.Close()
	}()

	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
			break
		}

		slave.Conn.WriteMessage(websocket.BinaryMessage, msg)
	}
}

func (rel *Relay) HandleSlave(conn *websocket.Conn) {
	idBytes := make([]byte, 8)
	rand.Read(idBytes)
	id := hex.EncodeToString(idBytes)

	s := &Slave{ID: id, Conn: conn}

	rel.mu.Lock()
	rel.slaves[s.ID] = s
	rel.mu.Unlock()

	db.LogSlaveConnected(id)

	// Send ID to slave immediately
	s.Conn.WriteMessage(websocket.TextMessage, []byte(s.ID))

	defer rel.RemoveSlave(s.ID)

	for {
		_, msg, err := s.Conn.ReadMessage()
		if err != nil {
			break
		}
		// Relay to Master if connected to THIS slave
		rel.mu.RLock()
		if rel.master != nil && rel.masterTarget == s.ID {
			rel.master.WriteMessage(websocket.BinaryMessage, msg)
		}
		rel.mu.RUnlock()
	}
}

func (rel *Relay) RemoveSlave(id string) {
	rel.mu.Lock()
	defer rel.mu.Unlock()
	if s, ok := rel.slaves[id]; ok {
		// TODO: Close the connection properly.
		// Write a control message, then wait for the client to acknowledge.

		s.Conn.Close()
		delete(rel.slaves, id)
		if rel.masterTarget == id && rel.master != nil {
			rel.master.Close()
		}

		db.LogSlaveDisconnected(id)
	}
}

func (rel *Relay) heartbeatTask() {
	ticker := time.NewTicker(30 * time.Second)
	for range ticker.C {
		rel.mu.RLock()
		for _, s := range rel.slaves {
			s.Conn.WriteMessage(websocket.TextMessage, []byte("HEARTBEAT"))
		}
		rel.mu.RUnlock()
	}
}

type WebServer struct {
	sCookie *securecookie.SecureCookie
	relay   *Relay
}

func NewWebServer(relay *Relay) *WebServer {
	// Autogenerate keys on startup
	hKey := make([]byte, 64)
	bKey := make([]byte, 32)
	rand.Read(hKey)
	rand.Read(bKey)

	sCookie := securecookie.New(hKey, bKey)
	sCookie.MaxAge(3600)

	return &WebServer{sCookie: sCookie, relay: relay}
}

func main() {
	_ = godotenv.Load()

	dbURL := os.Getenv("DATABASE_URL")
	if dbURL != "" {
		if err := db.Connect(dbURL); err != nil {
			log.Printf("Failed to connect to database: %v", err)
		}
		defer db.Close()
	}

	relay := NewRelay()
	server := NewWebServer(relay)

	mux := http.NewServeMux()

	mux.HandleFunc("GET /", server.handleIndex)
	mux.HandleFunc("GET /login", server.handleLoginPage)
	mux.HandleFunc("GET /dashboard", server.authMiddleware(server.handleDashboardPage))

	// WebSocket Endpoints
	mux.HandleFunc("GET /ws/slave", server.handleSlave)
	mux.HandleFunc("GET /ws/master", server.authMiddleware(server.handleMaster))

	// Admin API
	mux.HandleFunc("GET /api/health", server.handleHealth)
	mux.HandleFunc("POST /api/login", server.handleLogin)
	mux.HandleFunc("POST /api/logout", server.handleLogout)
	mux.HandleFunc("GET /api/slaves", server.authMiddleware(server.handleListSlaves))
	mux.HandleFunc("POST /api/slaves/{id}/kick", server.authMiddleware(server.handleKickSlave))
	mux.HandleFunc("GET /api/logs", server.authMiddleware(server.handleLogs))

	loggedMux := func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			log.Printf(
				"%s %s",
				r.Method,
				r.URL.Path,
			)

			next.ServeHTTP(w, r)
		})
	}(mux)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	addr := ":" + port

	log.Printf("Relay Server started on %s", addr)
	log.Fatal(http.ListenAndServe(addr, loggedMux))
}

// --- Auth Middleware ---

func (server *WebServer) handleLogin(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Password string `json:"token"`
	}

	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}

	storedHash := os.Getenv("AUTH_TOKEN")
	if storedHash == "" {
		log.Println("ERROR: AUTH_HASH environment variable not set")
		http.Error(w, "Server misconfiguration", http.StatusInternalServerError)
		return
	}

	err := bcrypt.CompareHashAndPassword([]byte(storedHash), []byte(body.Password))
	if err != nil {
		log.Printf("Failed login attempt: %v", err)
		http.Error(w, "Invalid credentials", http.StatusUnauthorized)
		return
	}

	// Create session valid for 1 hour
	if encoded, err := server.sCookie.Encode("session", "authenticated"); err == nil {
		http.SetCookie(w, &http.Cookie{
			Name:     "session",
			Value:    encoded,
			Path:     "/",
			HttpOnly: true,
			MaxAge:   3600, // 1 hour
		})

		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	}
}

func (server *WebServer) handleLogout(w http.ResponseWriter, r *http.Request) {
	// To delete a cookie, we set its MaxAge to -1
	http.SetCookie(w, &http.Cookie{
		Name:     "session",
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		MaxAge:   -1,
		Expires:  time.Unix(0, 0),
	})

	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status": "ok"}`))
}

func (server *WebServer) isAuthenticated(r *http.Request) bool {
	cookie, err := r.Cookie("session")
	if err != nil {
		return false
	}

	var value string
	if err := server.sCookie.Decode("session", cookie.Value, &value); err != nil {
		return false
	}

	return value == "authenticated"
}

func (server *WebServer) authMiddleware(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !server.isAuthenticated(r) {
			// If it's a page request, redirect. If it's an API call, 401.
			path := r.URL.Path
			if strings.HasPrefix(path, "/api/") || strings.HasPrefix(path, "/ws/") {
				http.Error(w, "Unauthorized", http.StatusUnauthorized)
			} else {
				http.Redirect(w, r, "/login", http.StatusSeeOther)
			}
			return
		}
		next(w, r)
	}
}

// --- API ---

func (server *WebServer) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"healthy"}`))
}

func (server *WebServer) handleListSlaves(w http.ResponseWriter, r *http.Request) {
	var ids []string
	server.relay.WithSlaves(func(slaves map[string]*Slave) {
		ids = make([]string, 0, len(slaves))
		for id := range slaves {
			ids = append(ids, id)
		}
	})

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string][]string{"slaves": ids})
}

func (server *WebServer) handleKickSlave(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	server.relay.RemoveSlave(id)
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"kicked"}`))
}

func (server *WebServer) handleLogs(w http.ResponseWriter, r *http.Request) {
	logs, err := db.GetLogs()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string][]db.LogEntry{"logs": logs})
}

// --- Relay Logic ---

func (server *WebServer) handleSlave(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}

	server.relay.HandleSlave(conn)
}

func (server *WebServer) handleMaster(w http.ResponseWriter, r *http.Request) {
	slaveID := r.URL.Query().Get("slave_id")

	if !server.relay.HasSlave(slaveID) {
		http.Error(w, "Slave offline", 4002)
		return
	}

	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}

	server.relay.HandleMaster(conn, slaveID)
}

func (server *WebServer) handleIndex(w http.ResponseWriter, r *http.Request) {
	if server.isAuthenticated(r) {
		http.Redirect(w, r, "/dashboard", http.StatusSeeOther)
	} else {
		http.Redirect(w, r, "/login", http.StatusSeeOther)
	}
}

func (server *WebServer) handleLoginPage(w http.ResponseWriter, r *http.Request) {
	// If already logged in, don't show login page
	if server.isAuthenticated(r) {
		http.Redirect(w, r, "/dashboard", http.StatusSeeOther)
		return
	}

	data, _ := content.ReadFile("static/login.html")
	w.Header().Set("Content-Type", "text/html")
	w.Write(data)
}

func (server *WebServer) handleDashboardPage(w http.ResponseWriter, r *http.Request) {
	data, _ := content.ReadFile("static/dashboard.html")
	w.Header().Set("Content-Type", "text/html")
	w.Write(data)
}
