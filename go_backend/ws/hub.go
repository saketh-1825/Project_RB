// Package ws implements the WebSocket hub for real-time dashboard communication.
// Contract: ws://go-backend:8080/ws
package ws

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/rs/zerolog/log"

	"sre-copilot/clients"
)

const (
	// pingInterval is how often the server sends a WebSocket ping frame.
	pingInterval = 10 * time.Second
	// pongWait is how long to wait for a pong before dropping the connection.
	pongWait = 15 * time.Second
	// writeWait is the maximum time allowed to write a message to the client.
	writeWait = 10 * time.Second
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
	hub           *Hub
	conn          *websocket.Conn
	send          chan []byte
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

	// LangGraph client for forwarding human_input events
	LangGraph *clients.LangGraphClient
}

// NewHub creates a new WebSocket hub.
func NewHub(langGraph *clients.LangGraphClient) *Hub {
	return &Hub{
		clients:    make(map[*Client]bool),
		broadcast:  make(chan []byte, 256),
		register:   make(chan *Client),
		unregister: make(chan *Client),
		LangGraph:  langGraph,
	}
}

// Run starts the hub's event loop. Call this in a goroutine.
func (h *Hub) Run() {
	ticker := time.NewTicker(pingInterval)
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
			// Broadcast server-time ping to keep connections alive
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
// Drops the connection if no pong is received within pongWait.
func (c *Client) readPump() {
	defer func() {
		c.hub.unregister <- c
		c.conn.Close()
	}()

	c.conn.SetReadDeadline(time.Now().Add(pongWait))
	c.conn.SetPongHandler(func(string) error {
		c.conn.SetReadDeadline(time.Now().Add(pongWait))
		return nil
	})

	for {
		_, raw, err := c.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Warn().Err(err).Msg("ws: unexpected close")
			}
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
					log.Info().Str("incident_id", id).Msg("ws: client subscribed to incident")
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

		case "human_input":
			// Forward human decision to LangGraph POST /analyses/:id/interrupt.
			// Contract payload: { analysis_id, interrupt_type, response_payload, provided_by }
			if c.hub.LangGraph == nil {
				break
			}
			payload, ok := msg.Payload.(map[string]interface{})
			if !ok {
				break
			}
			analysisID, _ := payload["analysis_id"].(string)
			interruptType, _ := payload["interrupt_type"].(string)
			providedBy, _ := payload["provided_by"].(string)
			responsePayload, _ := payload["response_payload"].(map[string]interface{})
			if analysisID == "" || interruptType == "" {
				log.Warn().Msg("ws: human_input missing analysis_id or interrupt_type")
				break
			}
			go func(aID, iType string, respPL map[string]interface{}, by string) {
				ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
				defer cancel()
				if err := c.hub.LangGraph.SendInterrupt(ctx, aID, iType, respPL, by); err != nil {
					log.Error().Err(err).Str("analysis_id", aID).Msg("ws: SendInterrupt failed")
				} else {
					log.Info().Str("analysis_id", aID).Str("interrupt_type", iType).Msg("ws: human_input forwarded to LangGraph")
				}
			}(analysisID, interruptType, responsePayload, providedBy)

		case "pong":
			c.conn.SetReadDeadline(time.Now().Add(pongWait))
		}
	}
}

// writePump sends messages from the hub to the client.
// Sends a WebSocket ping frame every pingInterval; drops the connection on write error.
func (c *Client) writePump() {
	ticker := time.NewTicker(pingInterval)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case msg, ok := <-c.send:
			c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if !ok {
				c.conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}
			if err := c.conn.WriteMessage(websocket.TextMessage, msg); err != nil {
				return
			}

		case <-ticker.C:
			c.conn.SetWriteDeadline(time.Now().Add(writeWait))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}
