package main

// The bridge: turns whatsmeow events from the listed agents' chats into
// agent-runner's shared event format, and sends for the runner. Events from
// any other chat are dropped here, in memory, before anything is written.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

const (
	trackWindow = 48 * time.Hour   // how long a message is watched for receipts, edits and reactions
	maxOnline   = 45 * time.Minute // "online" turns itself off after this, in case a run dies while waiting
)

// messenger is the part of *whatsmeow.Client the bridge uses.
type messenger interface {
	SendMessage(ctx context.Context, to types.JID, message *waE2E.Message, extra ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error)
	SendPresence(ctx context.Context, state types.Presence) error
	SubscribePresence(ctx context.Context, jid types.JID) error
	Download(ctx context.Context, msg whatsmeow.DownloadableMessage) ([]byte, error)
	IsConnected() bool
	IsLoggedIn() bool
}

type agentEntry struct {
	Name   string   `json:"name"`
	Handle string   `json:"handle"`
	JIDs   []string `json:"jids"`
	jids   []types.JID
}

type Bridge struct {
	dir     string
	client  messenger
	lidToPN func(context.Context, types.JID) (types.JID, error)
	now     func() time.Time
	logf    func(format string, args ...any)

	agentsMu    sync.Mutex
	agents      []agentEntry
	agentsMtime time.Time

	mu          sync.Mutex
	tracked     map[string]map[string]any // "agent:message id" → the last event written for it
	available   bool
	offTimer    *time.Timer
	status      string
}

func newBridge(dir string, client messenger, lidToPN func(context.Context, types.JID) (types.JID, error)) *Bridge {
	b := &Bridge{
		dir: dir, client: client, lidToPN: lidToPN, now: time.Now,
		logf:    func(format string, args ...any) { fmt.Fprintf(os.Stderr, time.Now().Format(time.RFC3339)+" "+format+"\n", args...) },
		tracked: map[string]map[string]any{},
		status:  "starting",
	}
	if data, err := os.ReadFile(b.path("state.json")); err == nil {
		_ = json.Unmarshal(data, &b.tracked)
	}
	return b
}

func (b *Bridge) path(name string) string { return filepath.Join(b.dir, name) }

func iso(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }

// MARK: Agents

// currentAgents re-reads agents.json when it changes, so adding an agent
// doesn't need a restart.
func (b *Bridge) currentAgents() []agentEntry {
	b.agentsMu.Lock()
	defer b.agentsMu.Unlock()
	info, err := os.Stat(b.path("agents.json"))
	if err != nil {
		b.agents = nil
		return nil
	}
	if info.ModTime().Equal(b.agentsMtime) {
		return b.agents
	}
	var agents []agentEntry
	data, err := os.ReadFile(b.path("agents.json"))
	if err == nil {
		err = json.Unmarshal(data, &agents)
	}
	if err != nil {
		b.logf("can't read agents.json: %v", err)
		return b.agents
	}
	for i := range agents {
		for _, s := range agents[i].JIDs {
			if jid, err := types.ParseJID(s); err == nil {
				agents[i].jids = append(agents[i].jids, jid)
			}
		}
	}
	b.agents, b.agentsMtime = agents, info.ModTime()
	return agents
}

// agentFor is the listed agent a 1:1 chat belongs to, or nil. WhatsApp may
// address a person by phone number or by private ID (LID), so both are checked.
func (b *Bridge) agentFor(ctx context.Context, src types.MessageSource) *agentEntry {
	if src.IsGroup {
		return nil
	}
	agents := b.currentAgents()
	candidates := []types.JID{src.Chat, src.SenderAlt, src.RecipientAlt}
	if src.Chat.Server == types.HiddenUserServer && b.lidToPN != nil {
		if pn, err := b.lidToPN(ctx, src.Chat); err == nil {
			candidates = append(candidates, pn)
		}
	}
	for i := range agents {
		for _, want := range agents[i].jids {
			for _, c := range candidates {
				if !c.IsEmpty() && c.User == want.User && c.Server == want.Server {
					return &agents[i]
				}
			}
		}
	}
	return nil
}

func (b *Bridge) agentByName(name string) *agentEntry {
	agents := b.currentAgents()
	for i := range agents {
		if agents[i].Name == name && len(agents[i].jids) > 0 {
			return &agents[i]
		}
	}
	return nil
}

// MARK: Writing events

func (b *Bridge) emit(kind string, event map[string]any) {
	line := map[string]any{"kind": kind, "emitted_at": iso(b.now())}
	for k, v := range event {
		line[k] = v
	}
	data, err := json.Marshal(line)
	if err != nil {
		b.logf("can't encode an event: %v", err)
		return
	}
	inbox := b.path("inbox/messages.jsonl")
	_ = os.MkdirAll(filepath.Dir(inbox), 0o700)
	f, err := os.OpenFile(inbox, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		b.logf("can't write the inbox: %v", err)
		return
	}
	defer f.Close()
	_, _ = f.Write(append(data, '\n'))
}

func (b *Bridge) saveState() {
	cutoff := iso(b.now().Add(-trackWindow))
	for key, ev := range b.tracked {
		if date, _ := ev["date"].(string); date < cutoff {
			delete(b.tracked, key)
		}
	}
	data, _ := json.Marshal(b.tracked)
	_ = os.WriteFile(b.path("state.json"), data, 0o600)
}

func sameEvent(a, b map[string]any) bool {
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return bytes.Equal(x, y)
}

// normalize round-trips an event through JSON, so stored and new events compare alike.
func normalize(event map[string]any) map[string]any {
	data, _ := json.Marshal(event)
	var out map[string]any
	_ = json.Unmarshal(data, &out)
	return out
}

// record writes "new" the first time a message is seen and "update" when it changes.
func (b *Bridge) record(key string, event map[string]any) {
	event = normalize(event)
	b.mu.Lock()
	defer b.mu.Unlock()
	prev, seen := b.tracked[key]
	if seen {
		for _, k := range []string{"date_delivered", "date_read", "date_played", "date_edited", "reactions"} {
			if v, ok := prev[k]; ok {
				if _, set := event[k]; !set {
					event[k] = v
				}
			}
		}
		if sameEvent(prev, event) {
			return
		}
	}
	b.tracked[key] = event
	b.saveState()
	b.emit(map[bool]string{true: "update", false: "new"}[seen], event)
}

// change applies fn to a tracked message and writes an "update" if fn reports a change.
func (b *Bridge) change(key string, fn func(event map[string]any) bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	prev, ok := b.tracked[key]
	if !ok {
		return
	}
	event := normalize(prev)
	if !fn(event) {
		return
	}
	b.tracked[key] = event
	b.saveState()
	b.emit("update", event)
}

func (b *Bridge) messageEvent(ctx context.Context, agent *agentEntry, info types.MessageInfo, m *waE2E.Message) map[string]any {
	event := map[string]any{
		"channel":     "whatsapp",
		"agent":       agent.Name,
		"handle":      agent.Handle,
		"guid":        "whatsapp:" + agent.Name + ":" + info.ID,
		"message_id":  info.ID,
		"is_from_me":  info.IsFromMe,
		"date":        iso(info.Timestamp),
		"kinds":       kinds(m),
		"attachments": b.attachments(ctx, info.ID, m),
	}
	if text := messageText(m); text != "" {
		event["text"] = text
	}
	if rows := messageButtons(m); len(rows) > 0 {
		event["buttons"] = rows
	}
	if preview := linkPreview(m); preview != nil {
		event["link_preview"] = preview
	}
	if target := replyTo(m); target != "" {
		event["reply_to"] = target
	}
	if r := raw(m); r != nil {
		event["raw"] = r
	}
	return event
}

var unsafeName = regexp.MustCompile(`[^A-Za-z0-9._-]`)

func (b *Bridge) attachments(ctx context.Context, id string, m *waE2E.Message) []map[string]any {
	media := messageMedia(m)
	if media == nil {
		return []map[string]any{}
	}
	name := media.fileName()
	info := map[string]any{"name": name, "mime_type": media.mimeType}
	rel := filepath.Join("attachments", unsafeName.ReplaceAllString(id, "_"), strings.TrimLeft(unsafeName.ReplaceAllString(name, "_"), "."))
	dest := b.path(filepath.Join("inbox", rel))
	if _, err := os.Stat(dest); err != nil {
		data, err := b.client.Download(ctx, media.part)
		if err != nil {
			b.logf("couldn't download an attachment of message %s: %v", id, err)
			return []map[string]any{info}
		}
		_ = os.MkdirAll(filepath.Dir(dest), 0o700)
		if err := os.WriteFile(dest, data, 0o600); err != nil {
			b.logf("couldn't save an attachment of message %s: %v", id, err)
			return []map[string]any{info}
		}
	}
	info["path"] = rel
	return []map[string]any{info}
}

// MARK: whatsmeow events

func (b *Bridge) handle(ctx context.Context, evt any) {
	switch e := evt.(type) {
	case *events.Message:
		b.onMessage(ctx, e)
	case *events.Receipt:
		b.onReceipt(ctx, e)
	case *events.ChatPresence:
		b.onTyping(ctx, e)
	case *events.UndecryptableMessage:
		if agent := b.agentFor(ctx, e.Info.MessageSource); agent != nil {
			b.logf("couldn't decrypt a message from %s; WhatsApp will ask your phone to resend it", agent.Name)
		}
	case *events.Connected:
		b.setStatus("connected")
		// Stay "offline" unless a run asks otherwise, so your phone keeps its notifications.
		_ = b.client.SendPresence(ctx, types.PresenceUnavailable)
	case *events.Disconnected:
		b.setStatus("disconnected")
	case *events.StreamReplaced:
		b.setStatus("replaced")
	case *events.LoggedOut:
		b.setStatus("logged_out")
	}
}

func (b *Bridge) onMessage(ctx context.Context, e *events.Message) {
	agent := b.agentFor(ctx, e.Info.MessageSource)
	if agent == nil {
		if e.Info.Chat.Server == types.BotServer && !e.Info.IsGroup {
			// Bot IDs aren't personal, and this is how you find an agent's ID.
			b.logf("ignored a message in a chat with bot account %s, which isn't in agents.json", e.Info.Chat)
		}
		return
	}
	m := e.Message
	if p := m.GetProtocolMessage(); p != nil {
		target := agent.Name + ":" + p.GetKey().GetID()
		switch p.GetType() {
		case waE2E.ProtocolMessage_REVOKE:
			b.mu.Lock()
			prev, ok := b.tracked[target]
			delete(b.tracked, target)
			b.saveState()
			b.mu.Unlock()
			if ok {
				deleted := normalize(prev)
				deleted["date_deleted"] = iso(e.Info.Timestamp)
				b.emit("deleted", deleted)
			}
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			// Muse streams its answers this way: one message, edited as it grows.
			edited := p.GetEditedMessage()
			b.change(target, func(ev map[string]any) bool {
				for _, k := range []string{"text", "buttons", "link_preview", "raw"} {
					delete(ev, k)
				}
				if text := messageText(edited); text != "" {
					ev["text"] = text
				}
				if rows := messageButtons(edited); len(rows) > 0 {
					ev["buttons"] = normalizeValue(rows)
				}
				if preview := linkPreview(edited); preview != nil {
					ev["link_preview"] = normalizeValue(preview)
				}
				if r := raw(edited); r != nil {
					ev["raw"] = normalizeValue(r)
				}
				ev["kinds"] = normalizeValue(kinds(edited))
				ev["date_edited"] = iso(e.Info.Timestamp)
				return true
			})
		}
		return
	}
	if r := m.GetReactionMessage(); r != nil {
		who := map[bool]string{true: "me", false: "agent"}[e.Info.IsFromMe]
		b.change(agent.Name+":"+r.GetKey().GetID(), func(ev map[string]any) bool {
			var kept []any
			if list, ok := ev["reactions"].([]any); ok {
				for _, item := range list {
					if entry, ok := item.(map[string]any); ok && entry["by"] != who {
						kept = append(kept, entry)
					}
				}
			}
			if r.GetText() != "" {
				kept = append(kept, map[string]any{"emoji": r.GetText(), "by": who})
			}
			ev["reactions"] = kept
			return true
		})
		return
	}
	b.record(agent.Name+":"+e.Info.ID, b.messageEvent(ctx, agent, e.Info, m))
}

func normalizeValue(v any) any {
	data, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(data, &out)
	return out
}

func (b *Bridge) onReceipt(ctx context.Context, e *events.Receipt) {
	field := map[types.ReceiptType]string{
		types.ReceiptTypeDelivered: "date_delivered",
		types.ReceiptTypeRead:      "date_read",
		types.ReceiptTypePlayed:    "date_played",
	}[e.Type]
	if field == "" || e.IsFromMe {
		return // receipts from your own devices don't say anything about the agent
	}
	agent := b.agentFor(ctx, e.MessageSource)
	if agent == nil {
		return
	}
	for _, id := range e.MessageIDs {
		b.change(agent.Name+":"+id, func(ev map[string]any) bool {
			if ev["is_from_me"] != true || ev[field] != nil {
				return false
			}
			ev[field] = iso(e.Timestamp)
			return true
		})
	}
}

func (b *Bridge) onTyping(ctx context.Context, e *events.ChatPresence) {
	agent := b.agentFor(ctx, e.MessageSource)
	if agent == nil {
		return
	}
	action := "typing"
	if e.State == types.ChatPresencePaused {
		action = "paused"
	} else if e.Media == types.ChatPresenceMediaAudio {
		action = "recording"
	}
	b.emit("typing", map[string]any{"channel": "whatsapp", "agent": agent.Name, "handle": agent.Handle, "action": action})
}

// MARK: What the runner asks for

func (b *Bridge) Send(ctx context.Context, name, text string) (map[string]any, error) {
	agent := b.agentByName(name)
	if agent == nil {
		return nil, fmt.Errorf("%q isn't in agents.json, so the bridge won't message it", name)
	}
	if strings.TrimSpace(text) == "" {
		return nil, errors.New("refusing to send an empty message")
	}
	if !b.client.IsLoggedIn() {
		return nil, errors.New("not linked to WhatsApp; run: uv run bench whatsapp-link")
	}
	resp, err := b.client.SendMessage(ctx, agent.jids[0], &waE2E.Message{Conversation: proto.String(text)})
	if err != nil {
		return nil, err
	}
	info := types.MessageInfo{ID: resp.ID, Timestamp: resp.Timestamp, MessageSource: types.MessageSource{Chat: agent.jids[0], IsFromMe: true}}
	event := b.messageEvent(ctx, agent, info, &waE2E.Message{Conversation: proto.String(text)})
	b.record(agent.Name+":"+resp.ID, event)
	return normalize(event), nil
}

// SetAvailable shows you as online (so agents' typing reaches the bridge) or offline.
func (b *Bridge) SetAvailable(ctx context.Context, on bool) error {
	b.mu.Lock()
	if b.offTimer != nil {
		b.offTimer.Stop()
		b.offTimer = nil
	}
	if on {
		b.offTimer = time.AfterFunc(maxOnline, func() { _ = b.SetAvailable(context.Background(), false) })
	}
	b.available = on
	b.mu.Unlock()
	if !on {
		return b.client.SendPresence(ctx, types.PresenceUnavailable)
	}
	if err := b.client.SendPresence(ctx, types.PresenceAvailable); err != nil {
		return err
	}
	for _, agent := range b.currentAgents() {
		for _, jid := range agent.jids {
			_ = b.client.SubscribePresence(ctx, jid) // bots may not support it; typing still arrives in the chat
		}
	}
	return nil
}

func (b *Bridge) setStatus(status string) {
	b.mu.Lock()
	b.status = status
	b.mu.Unlock()
	data, _ := json.Marshal(map[string]string{"status": status, "since": iso(b.now())})
	_ = os.WriteFile(b.path("status.json"), data, 0o600)
}

func (b *Bridge) Status() map[string]any {
	agents := b.currentAgents()
	b.mu.Lock()
	defer b.mu.Unlock()
	names := []string{}
	for _, a := range agents {
		names = append(names, a.Name)
	}
	return map[string]any{
		"ok": true, "status": b.status, "connected": b.client.IsConnected(), "logged_in": b.client.IsLoggedIn(),
		"agents": names, "online": b.available,
	}
}
