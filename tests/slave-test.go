package main

import (
	"bufio"
	"fmt"
	"log"
	"os"

	"github.com/gorilla/websocket"
	"github.com/joho/godotenv"
)

func main() {
	godotenv.Load()
	wsURL := os.Getenv("WS_HOST")
	if wsURL == "" {
		wsURL = "ws://localhost:8080"
	}

	conn, _, err := websocket.DefaultDialer.Dial(wsURL+"/ws/slave", nil)
	if err != nil {
		log.Fatalf("Failed to connect: %v", err)
	}
	defer conn.Close()

	_, msg, err := conn.ReadMessage()
	if err != nil {
		log.Fatalf("Failed to read slave ID: %v", err)
	}
	fmt.Printf("Connected with ID: %s\n", msg)

	scanner := bufio.NewScanner(os.Stdin)

	go func() {
		for {
			_, msg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			fmt.Printf("Received: %s\n", msg)
		}
	}()

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
