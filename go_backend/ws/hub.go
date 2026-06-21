// Package ws implements the WebSocket hub for real-time dashboard communication.
// Contract: ws://go-backend:8080/ws
package ws

import (
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog/log"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true }, // Allow all origins in dev
}

// Message is the WebSocket message envelope per contract.
type Message struct {
	Event     string      `json:"event"`
	Payload   interface{} `json:"payload"`
	Timestamp string      `json:"timestamp"`
	RequestID *string     `json:"request_id,omitempty"`
}

// Client represents a single WebSocket connection.
type Client struct {
	hub  *Hub
	conn *websocket.Conn
	send chan []byte
	// Incidents this client is subscribed to for granular updates
	subscriptions map[string]bool
	mu            sync.RWMutex
}

// Hub manages all WebSocket connections and broadcasts.
type Hub struct {
	clients    map[*Client]bool
	broadcast  chan []byte
	register   chan *Client
	unregister chan *Client
	mu         sync.RWMutex
}

// NewHub creates a new WebSocket hub.
func NewHub() *Hub {
	return &Hub{
		clients:    make(map[*Client]bool),
		broadcast:  make(chan []byte, 256),
		register:   make(chan *Client),
		unregister: make(chan *Client),
	}
}

// Run starts the hub's event loop. Call this in a goroutine.
func (h *Hub) Run() {
	ticker := time.NewTicker(30 * time.Second) // ping interval
	defer ticker.Stop()

	for {
		select {
		case client := <-h.register:
			h.mu.Lock()
			h.clients[client] = true
			h.mu.Unlock()
			log.Info().Msg("ws: client connected")

		case client := <-h.unregister:
			h.mu.Lock()
			if _, ok := h.clients[client]; ok {
				delete(h.clients, client)
				close(client.send)
			}
			h.mu.Unlock()
			log.Info().Msg("ws: client disconnected")

		case msg := <-h.broadcast:
			h.mu.RLock()
			for client := range h.clients {
				select {
				case client.send <- msg:
				default:
					close(client.send)
					delete(h.clients, client)
				}
			}
			h.mu.RUnlock()

		case <-ticker.C:
			// Send ping to all clients
			h.BroadcastEvent("ping", map[string]interface{}{
				"server_time": time.Now().UTC().Format(time.RFC3339Nano),
			})
		}
	}
}

// BroadcastEvent sends an event to all connected clients.
func (h *Hub) BroadcastEvent(event string, payload interface{}) {
	msg := Message{
		Event:     event,
		Payload:   payload,
		Timestamp: time.Now().UTC().Format(time.RFC3339Nano),
	}
	data, err := json.Marshal(msg)
	if err != nil {
		log.Error().Err(err).Str("event", event).Msg("ws: failed to marshal broadcast")
		return
	}
	h.broadcast <- data
}

// BroadcastToIncident sends an event only to clients subscribed to a specific incident.
func (h *Hub) BroadcastToIncident(incidentID, event string, payload interface{}) {
	msg := Message{
		Event:     event,
		Payload:   payload,
		Timestamp: time.Now().UTC().Format(time.RFC3339Nano),
	}
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}

	h.mu.RLock()
	defer h.mu.RUnlock()
	for client := range h.clients {
		client.mu.RLock()
		subscribed := client.subscriptions[incidentID]
		client.mu.RUnlock()
		if subscribed {
			select {
			case client.send <- data:
			default:
			}
		}
	}
}

// HandleWebSocket is the Gin handler for upgrading HTTP to WebSocket.
func (h *Hub) HandleWebSocket(c *gin.Context) {
	conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Error().Err(err).Msg("ws: upgrade failed")
		return
	}

	client := &Client{
		hub:           h,
		conn:          conn,
		send:          make(chan []byte, 256),
		subscriptions: make(map[string]bool),
	}
	h.register <- client

	go client.writePump()
	go client.readPump()
}

// readPump reads messages from the client (subscribe, unsubscribe, human_input, pong).
func (c *Client) readPump() {
	defer func() {
		c.hub.unregister <- c
		c.conn.Close()
	}()

	c.conn.SetReadDeadline(time.Now().Add(60 * time.Second))
	c.conn.SetPongHandler(func(string) error {
		c.conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		return nil
	})

	for {
		_, raw, err := c.conn.ReadMessage()
		if err != nil {
			break
		}

		var msg Message
		if err := json.Unmarshal(raw, &msg); err != nil {
			continue
		}

		switch msg.Event {
		case "subscribe.incident":
			if payload, ok := msg.Payload.(map[string]interface{}); ok {
				if id, ok := payload["incident_id"].(string); ok {
					c.mu.Lock()
					c.subscriptions[id] = true
					c.mu.Unlock()
				}
			}
		case "unsubscribe.incident":
			if payload, ok := msg.Payload.(map[string]interface{}); ok {
				if id, ok := payload["incident_id"].(string); ok {
					c.mu.Lock()
					delete(c.subscriptions, id)
					c.mu.Unlock()
				}
			}
		case "pong":
			// Keepalive acknowledged
			c.conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		}
	}
}

// writePump sends messages from the hub to the client.
func (c *Client) writePump() {
	ticker := time.NewTicker(30 * time.Second)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case msg, ok := <-c.send:
			if !ok {
				c.conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}
		case <-ticker.C:
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}
