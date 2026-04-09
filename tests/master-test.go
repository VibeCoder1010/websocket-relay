package main

import (
	"bufio"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"

	"github.com/gorilla/websocket"
	"github.com/joho/godotenv"
	"golang.org/x/term"
)

var (
	wsURL   string
	httpURL string
)

func init() {
	godotenv.Load()
	wsURL = os.Getenv("WS_HOST")
	if wsURL == "" {
		wsURL = "ws://localhost:8080"
	}
	httpURL = os.Getenv("HTTP_HOST")
	if httpURL == "" {
		httpURL = "http://localhost:8080"
	}
}

func login(token string) (string, error) {
	resp, err := http.Post(httpURL+"/api/login", "application/json", strings.NewReader(fmt.Sprintf(`{"token":"%s"}`, token)))
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("login failed with status: %d", resp.StatusCode)
	}

	cookies := resp.Cookies()
	for _, c := range cookies {
		if c.Name == "session" {
			return c.Value, nil
		}
	}
	return "", fmt.Errorf("no session cookie found")
}

func connectMaster(slaveID, sessionToken string) {
	header := http.Header{}
	header.Add("Cookie", fmt.Sprintf("session=%s", sessionToken))

	urlStr := fmt.Sprintf("%s/ws/master?slave_id=%s", wsURL, slaveID)
	conn, _, err := websocket.DefaultDialer.Dial(urlStr, header)
	if err != nil {
		log.Fatalf("Failed to connect: %v", err)
	}
	defer conn.Close()

	fmt.Printf("Connected to slave: %s\n", slaveID)

	go func() {
		for {
			_, msg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			fmt.Printf("Received: %s\n", msg)
		}
	}()

	scanner := bufio.NewScanner(os.Stdin)
	for {
		fmt.Print("Message: ")
		if !scanner.Scan() {
			return
		}
		input := scanner.Text()
		if err := conn.WriteMessage(websocket.TextMessage, []byte(input)); err != nil {
			return
		}
	}
}

func main() {
	fmt.Print("Enter auth token: ")
	tokenBytes, _ := term.ReadPassword(int(os.Stdin.Fd()))
	token := string(tokenBytes)
	fmt.Println()

	sessionToken, err := login(token)
	if err != nil {
		log.Fatalf("Login failed: %v", err)
		os.Exit(1)
	}
	fmt.Println("Login successful")

	var slaveID string
	fmt.Print("Enter slave ID: ")
	fmt.Scanln(&slaveID)

	connectMaster(slaveID, sessionToken)
}
