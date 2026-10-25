package main

// The local socket the runner talks to: one JSON request per connection, one
// JSON reply. Only your user account can open it.

import (
	"bufio"
	"context"
	"encoding/json"
	"net"
	"os"
	"time"
)

type request struct {
	Op     string `json:"op"` // "status", "send" or "presence"
	Agent  string `json:"agent,omitempty"`
	Text   string `json:"text,omitempty"`
	Online *bool  `json:"online,omitempty"`
}

func (b *Bridge) handleRequest(ctx context.Context, req request) map[string]any {
	fail := func(err error) map[string]any { return map[string]any{"ok": false, "error": err.Error()} }
	switch req.Op {
	case "status":
		return b.Status()
	case "send":
		event, err := b.Send(ctx, req.Agent, req.Text)
		if err != nil {
			return fail(err)
		}
		return map[string]any{"ok": true, "event": event}
	case "presence":
		if req.Online == nil {
			return map[string]any{"ok": false, "error": "presence needs online: true or false"}
		}
		if err := b.SetAvailable(ctx, *req.Online); err != nil {
			return fail(err)
		}
		return map[string]any{"ok": true, "online": *req.Online}
	}
	return map[string]any{"ok": false, "error": "unknown op " + req.Op}
}

func (b *Bridge) serve(ctx context.Context, path string) error {
	_ = os.Remove(path)
	listener, err := net.Listen("unix", path)
	if err != nil {
		return err
	}
	if err := os.Chmod(path, 0o600); err != nil {
		listener.Close()
		return err
	}
	go func() {
		<-ctx.Done()
		listener.Close()
	}()
	for {
		conn, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go func() {
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(90 * time.Second))
			var req request
			reply := map[string]any{"ok": false, "error": "couldn't read the request"}
			if line, err := bufio.NewReader(conn).ReadBytes('\n'); err == nil && json.Unmarshal(line, &req) == nil {
				reply = b.handleRequest(ctx, req)
			}
			_ = json.NewEncoder(conn).Encode(reply)
		}()
	}
}
