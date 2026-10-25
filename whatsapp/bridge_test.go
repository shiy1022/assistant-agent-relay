package main

import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAICommon"
	"go.mau.fi/whatsmeow/proto/waAICommonDeprecated"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

var (
	museJID     = types.MuseJID
	instinctJID = types.NewJID("15550100009", types.DefaultUserServer)
	instinctLID = types.NewJID("98765432101234", types.HiddenUserServer)
	strangerJID = types.NewJID("15559999999", types.DefaultUserServer)
	groupJID    = types.NewJID("120363000000000000", types.GroupServer)
	otherBotJID = types.NewMetaAIJID
	sentAt      = time.Date(2026, 10, 25, 4, 0, 0, 0, time.UTC)
)

type fakeClient struct {
	sent      []types.JID
	presence  []types.Presence
	loggedIn  bool
	nextID    int
	downloads int
}

func (f *fakeClient) SendMessage(ctx context.Context, to types.JID, m *waE2E.Message, extra ...whatsmeow.SendRequestExtra) (whatsmeow.SendResponse, error) {
	f.sent = append(f.sent, to)
	f.nextID++
	return whatsmeow.SendResponse{ID: types.MessageID("OUT" + string(rune('0'+f.nextID))), Timestamp: sentAt}, nil
}
func (f *fakeClient) SendPresence(ctx context.Context, p types.Presence) error {
	f.presence = append(f.presence, p)
	return nil
}
func (f *fakeClient) SubscribePresence(ctx context.Context, jid types.JID) error { return nil }
func (f *fakeClient) Download(ctx context.Context, m whatsmeow.DownloadableMessage) ([]byte, error) {
	f.downloads++
	return []byte("OggS voice note"), nil
}
func (f *fakeClient) IsConnected() bool { return true }
func (f *fakeClient) IsLoggedIn() bool  { return f.loggedIn }

func setup(t *testing.T) (*Bridge, *fakeClient) {
	t.Helper()
	dir := t.TempDir()
	agents := `[{"name":"muse","handle":"` + museJID.String() + `","jids":["` + museJID.String() + `"]},
	            {"name":"instinct","handle":"+15550100009","jids":["` + instinctJID.String() + `"]}]`
	if err := os.WriteFile(filepath.Join(dir, "agents.json"), []byte(agents), 0o600); err != nil {
		t.Fatal(err)
	}
	client := &fakeClient{loggedIn: true}
	lids := func(ctx context.Context, lid types.JID) (types.JID, error) {
		if lid == instinctLID {
			return instinctJID, nil
		}
		return types.JID{}, errors.New("unknown")
	}
	b := newBridge(dir, client, lids)
	b.logf = func(string, ...any) {}
	return b, client
}

func inbox(t *testing.T, b *Bridge) []map[string]any {
	t.Helper()
	f, err := os.Open(b.path("inbox/messages.jsonl"))
	if os.IsNotExist(err) {
		return nil
	} else if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []map[string]any
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 1<<20), 1<<20)
	for scanner.Scan() {
		var ev map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &ev); err != nil {
			t.Fatal(err)
		}
		out = append(out, ev)
	}
	return out
}

func incoming(chat types.JID, id string, m *waE2E.Message, opts ...func(*types.MessageInfo)) *events.Message {
	info := types.MessageInfo{ID: id, Timestamp: sentAt.Add(time.Minute), MessageSource: types.MessageSource{Chat: chat, Sender: chat}}
	for _, opt := range opts {
		opt(&info)
	}
	return &events.Message{Info: info, Message: m}
}

func text(s string) *waE2E.Message { return &waE2E.Message{Conversation: proto.String(s)} }

func TestOnlyAgentChatsAreRecorded(t *testing.T) {
	b, _ := setup(t)
	ctx := context.Background()
	b.handle(ctx, incoming(strangerJID, "S1", text("a friend's message")))
	b.handle(ctx, incoming(groupJID, "G1", text("group chat with an agent in it"), func(i *types.MessageInfo) {
		i.IsGroup, i.Sender = true, museJID
	}))
	b.handle(ctx, incoming(otherBotJID, "B1", text("Meta AI, not on the list")))
	b.handle(ctx, incoming(museJID, "M1", text("hi from Muse")))
	// Instinct addressed by its private ID, matched through the LID map.
	b.handle(ctx, incoming(instinctLID, "I1", text("hi from Instinct")))

	got := inbox(t, b)
	if len(got) != 2 || got[0]["agent"] != "muse" || got[1]["agent"] != "instinct" {
		t.Fatalf("recorded %v", got)
	}
	for _, ev := range got {
		if strings.Contains(ev["text"].(string), "friend") || strings.Contains(ev["text"].(string), "group") {
			t.Fatalf("recorded another chat: %v", ev)
		}
	}
	if got[0]["guid"] != "whatsapp:muse:M1" || got[0]["kind"] != "new" || got[0]["date"] != "2026-10-25T04:01:00.000Z" {
		t.Fatalf("unexpected event %v", got[0])
	}
}

func TestMuseRichResponsesBecomeText(t *testing.T) {
	b, _ := setup(t)
	reply := &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{
		Submessages: []*waAICommonDeprecated.AIRichResponseSubMessage{
			{MessageText: proto.String("Here's a plan for Tokyo:")},
			{MessageText: proto.String("Day 1: Asakusa and Ueno")},
		},
	}}
	b.handle(context.Background(), incoming(museJID, "M2", reply))
	ev := inbox(t, b)[0]
	if ev["text"] != "Here's a plan for Tokyo:\nDay 1: Asakusa and Ueno" {
		t.Fatalf("text = %q", ev["text"])
	}
	if ev["raw"] == nil {
		t.Fatal("an unfamiliar message type should keep its raw form")
	}
}

// unified wraps text the way Muse sends it: markdown primitives inside a unified response.
func unified(texts ...string) *waE2E.Message {
	var sections []string
	for _, t := range texts {
		quoted, _ := json.Marshal(t)
		sections = append(sections, `{"view_model":{"__typename":"GenAISingleLayoutViewModel","primitive":{"__typename":"GenAIMarkdownTextUXPrimitive","text":`+string(quoted)+`}}}`)
	}
	data := `{"response_id":"fc94d796","sections":[` + strings.Join(sections, ",") + `]}`
	return &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{
		MessageType:     waAICommonDeprecated.AIRichResponseMessageType_AI_RICH_RESPONSE_TYPE_STANDARD.Enum(),
		UnifiedResponse: &waAICommon.AIRichResponseUnifiedResponse{Data: []byte(data)},
	}}
}

func TestMuseStreamsItsAnswerByEditing(t *testing.T) {
	b, _ := setup(t)
	ctx := context.Background()
	b.handle(ctx, incoming(museJID, "M8", unified("Hey! Yep, I'm")))
	b.handle(ctx, incoming(museJID, "M9", &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("M8")},
		EditedMessage: unified("Hey! Yep, I'm here.", "**Want to plan a trip?**"),
	}}))
	got := inbox(t, b)
	if len(got) != 2 || got[0]["text"] != "Hey! Yep, I'm" {
		t.Fatalf("events = %v", got)
	}
	if got[1]["text"] != "Hey! Yep, I'm here.\n**Want to plan a trip?**" || got[1]["date_edited"] == nil {
		t.Fatalf("edit = %v", got[1])
	}
	// The raw form is protobuf JSON, so the unified response's bytes are base64.
	encoded := got[1]["raw"].(map[string]any)["richResponseMessage"].(map[string]any)["unifiedResponse"].(map[string]any)["data"].(string)
	if data, _ := base64.StdEncoding.DecodeString(encoded); !strings.Contains(string(data), "plan a trip") {
		t.Fatal("the edit's raw form should replace the first version's")
	}
}

func TestUnknownTypesStillYieldTheirWords(t *testing.T) {
	m := &waE2E.Message{InteractiveMessage: &waE2E.InteractiveMessage{
		Body: &waE2E.InteractiveMessage_Body{Text: proto.String("Pick a hotel")},
		InteractiveMessage: &waE2E.InteractiveMessage_NativeFlowMessage_{NativeFlowMessage: &waE2E.InteractiveMessage_NativeFlowMessage{
			Buttons: []*waE2E.InteractiveMessage_NativeFlowMessage_NativeFlowButton{
				{Name: proto.String("quick_reply"), ButtonParamsJSON: proto.String(`{"display_text":"Hotel A"}`)},
				{Name: proto.String("cta_url"), ButtonParamsJSON: proto.String(`{"display_text":"Details","url":"https://example.com"}`)},
			},
		}},
	}}
	if got := messageText(m); got != "Pick a hotel" {
		t.Fatalf("text = %q", got)
	}
	rows := messageButtons(m)
	if len(rows) != 1 || rows[0][0] != (button{"Hotel A", "quick_reply", ""}) || rows[0][1].URL != "https://example.com" {
		t.Fatalf("buttons = %v", rows)
	}
}

func TestSendReceiptsEditsDeletesAndReactions(t *testing.T) {
	b, client := setup(t)
	ctx := context.Background()
	sent, err := b.Send(ctx, "muse", "Plan a trip to Tokyo")
	if err != nil {
		t.Fatal(err)
	}
	id := sent["message_id"].(string)
	if client.sent[0] != museJID || sent["is_from_me"] != true || sent["date"] != "2026-10-25T04:00:00.000Z" {
		t.Fatalf("sent %v to %v", sent, client.sent)
	}
	receipt := func(kind types.ReceiptType, at time.Time) *events.Receipt {
		return &events.Receipt{MessageSource: types.MessageSource{Chat: museJID}, MessageIDs: []types.MessageID{id, "NOT-OURS"}, Timestamp: at, Type: kind}
	}
	b.handle(ctx, receipt(types.ReceiptTypeDelivered, sentAt.Add(time.Second)))
	b.handle(ctx, receipt(types.ReceiptTypeDelivered, sentAt.Add(time.Hour))) // a repeat changes nothing
	b.handle(ctx, receipt(types.ReceiptTypeRead, sentAt.Add(2*time.Second)))

	b.handle(ctx, incoming(museJID, "M3", text("Flights from $1,240")))
	b.handle(ctx, incoming(museJID, "M4", &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("M3")},
		EditedMessage: text("Flights from $1,190"),
	}}))
	b.handle(ctx, incoming(museJID, "M5", &waE2E.Message{ReactionMessage: &waE2E.ReactionMessage{
		Key: &waCommon.MessageKey{ID: proto.String(id)}, Text: proto.String("👍"),
	}}))
	b.handle(ctx, incoming(museJID, "M6", &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type: waE2E.ProtocolMessage_REVOKE.Enum(), Key: &waCommon.MessageKey{ID: proto.String("M3")},
	}}))

	got := inbox(t, b)
	var kinds []string
	for _, ev := range got {
		kinds = append(kinds, ev["kind"].(string))
	}
	if strings.Join(kinds, ",") != "new,update,update,new,update,update,deleted" {
		t.Fatalf("kinds = %v", kinds)
	}
	if got[1]["date_delivered"] != "2026-10-25T04:00:01.000Z" || got[2]["date_read"] != "2026-10-25T04:00:02.000Z" {
		t.Fatalf("receipts = %v / %v", got[1], got[2])
	}
	if got[4]["text"] != "Flights from $1,190" || got[4]["date_edited"] == nil {
		t.Fatalf("edit = %v", got[4])
	}
	reactions := got[5]["reactions"].([]any)
	if reactions[0].(map[string]any)["emoji"] != "👍" || got[5]["date_read"] == nil {
		t.Fatalf("reaction = %v", got[5])
	}
	if got[6]["message_id"] != "M3" || got[6]["date_deleted"] == nil {
		t.Fatalf("deleted = %v", got[6])
	}
}

func TestSendRefusals(t *testing.T) {
	b, client := setup(t)
	ctx := context.Background()
	for _, tc := range []struct{ agent, text, want string }{
		{"boba", "hi", "isn't in agents.json"},
		{"muse", "   ", "empty"},
	} {
		if _, err := b.Send(ctx, tc.agent, tc.text); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Fatalf("Send(%q, %q) = %v", tc.agent, tc.text, err)
		}
	}
	client.loggedIn = false
	if _, err := b.Send(ctx, "muse", "hi"); err == nil || !strings.Contains(err.Error(), "not linked") {
		t.Fatalf("Send while unlinked = %v", err)
	}
	if len(client.sent) != 0 {
		t.Fatalf("sent %v", client.sent)
	}
	if reply := b.handleRequest(ctx, request{Op: "mark_read"}); reply["ok"] != false {
		t.Fatalf("unknown op answered %v", reply)
	}
}

func TestTypingAndVoiceNotes(t *testing.T) {
	b, client := setup(t)
	ctx := context.Background()
	b.handle(ctx, &events.ChatPresence{MessageSource: types.MessageSource{Chat: museJID}, State: types.ChatPresenceComposing})
	b.handle(ctx, &events.ChatPresence{MessageSource: types.MessageSource{Chat: strangerJID}, State: types.ChatPresenceComposing})
	b.handle(ctx, &events.ChatPresence{MessageSource: types.MessageSource{Chat: museJID}, State: types.ChatPresenceComposing, Media: types.ChatPresenceMediaAudio})
	b.handle(ctx, incoming(museJID, "M7", &waE2E.Message{AudioMessage: &waE2E.AudioMessage{
		PTT: proto.Bool(true), Mimetype: proto.String("audio/ogg; codecs=opus"),
	}}))
	got := inbox(t, b)
	if len(got) != 3 || got[0]["action"] != "typing" || got[1]["action"] != "recording" {
		t.Fatalf("events = %v", got)
	}
	attachment := got[2]["attachments"].([]any)[0].(map[string]any)
	path := b.path(filepath.Join("inbox", attachment["path"].(string)))
	if data, _ := os.ReadFile(path); string(data) != "OggS voice note" || attachment["name"] != "voice.ogg" {
		t.Fatalf("attachment %v at %s", attachment, path)
	}
	if info, _ := os.Stat(path); info.Mode().Perm() != 0o600 {
		t.Fatalf("attachment mode %v", info.Mode())
	}
	if client.downloads != 1 {
		t.Fatalf("downloads = %d", client.downloads)
	}
}

func TestPresenceOnlyWhenAsked(t *testing.T) {
	b, client := setup(t)
	ctx := context.Background()
	b.handle(ctx, &events.Connected{})
	on, off := true, false
	b.handleRequest(ctx, request{Op: "presence", Online: &on})
	b.handleRequest(ctx, request{Op: "presence", Online: &off})
	want := []types.Presence{types.PresenceUnavailable, types.PresenceAvailable, types.PresenceUnavailable}
	if len(client.presence) != 3 || client.presence[0] != want[0] || client.presence[1] != want[1] || client.presence[2] != want[2] {
		t.Fatalf("presence = %v", client.presence)
	}
}

func TestFindingAnAgentByName(t *testing.T) {
	b, _ := setup(t)
	// Instinct isn't on the list yet; only Muse is.
	muse := `[{"name":"muse","handle":"` + museJID.String() + `","jids":["` + museJID.String() + `"]}]`
	_ = os.WriteFile(b.path("agents.json"), []byte(muse), 0o600)
	_ = os.Chtimes(b.path("agents.json"), time.Now(), time.Now().Add(time.Second))
	ctx := context.Background()
	named := func(name string) func(*types.MessageInfo) {
		return func(i *types.MessageInfo) { i.PushName = name }
	}

	b.handle(ctx, incoming(instinctLID, "X0", text("before looking"), named("Instinct")))
	b.StartFind("instinct", time.Minute)
	b.handle(ctx, incoming(strangerJID, "X1", text("a friend"), named("Alice")))
	b.handle(ctx, incoming(instinctLID, "X2", text("I'm here"), named("Me"), func(i *types.MessageInfo) { i.IsFromMe = true }))
	b.handle(ctx, incoming(instinctLID, "X3", text("Hi! I'm Instinct"), named("Instinct"), func(i *types.MessageInfo) {
		i.SenderAlt = instinctJID
	}))
	b.handle(ctx, incoming(instinctLID, "X4", text("again"), named("Instinct")))

	found, active := b.Found()
	if !active || len(found) != 1 {
		t.Fatalf("found %v (active %v)", found, active)
	}
	if found[0]["phone"] != "+15550100009" || found[0]["lid"] != instinctLID.String() || found[0]["push_name"] != "Instinct" {
		t.Fatalf("match = %v", found[0])
	}
	if got := inbox(t, b); len(got) != 0 {
		t.Fatalf("finding must not record messages: %v", got)
	}
	b.now = func() time.Time { return time.Now().Add(2 * time.Minute) }
	if _, active := b.Found(); active {
		t.Fatal("finding should stop when its time is up")
	}
}

func TestNewDevicesSkipHistory(t *testing.T) {
	dir := t.TempDir()
	device, err := openDevice(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	client := newClient(device)
	if !client.ManualHistorySyncDownload || store.DeviceProps.GetRequireFullSync() || store.DeviceProps.GetHistorySyncConfig().GetFullSyncDaysLimit() != 1 {
		t.Fatal("history download should be off and the request minimal")
	}
	if info, _ := os.Stat(filepath.Join(dir, "session.db")); info.Mode().Perm() != 0o600 {
		t.Fatalf("session.db mode %v", info.Mode())
	}
}

func TestSocket(t *testing.T) {
	b, _ := setup(t)
	sockDir, err := os.MkdirTemp("", "bw") // short path: Unix socket paths are limited to 104 bytes
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(sockDir)
	path := filepath.Join(sockDir, "bench.sock")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go b.serve(ctx, path)
	var conn net.Conn
	for i := 0; i < 50; i++ {
		if conn, err = net.Dial("unix", path); err == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if info, _ := os.Stat(path); info.Mode().Perm() != 0o600 {
		t.Fatalf("socket mode %v", info.Mode())
	}
	_, _ = conn.Write([]byte(`{"op":"send","agent":"stranger","text":"hi"}` + "\n"))
	var reply map[string]any
	if err := json.NewDecoder(conn).Decode(&reply); err != nil {
		t.Fatal(err)
	}
	if reply["ok"] != false || !strings.Contains(reply["error"].(string), "isn't in agents.json") {
		t.Fatalf("reply = %v", reply)
	}
}
