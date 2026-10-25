// bench-whatsapp: agent-runner's WhatsApp bridge.
//
// It's a linked device on your WhatsApp account (whatsmeow) that only ever
// records, or messages, the agents in agents.json, which `bench
// whatsapp-install` writes from config/agents.yaml. Everything else your
// account receives is dropped in memory. The history WhatsApp offers a new
// device isn't downloaded at all, it never sends read receipts, and it shows
// you as online only while a run asks it to (so agents' "typing…" arrives).
//
//	bench-whatsapp link     print QR codes (as JSON lines) until your phone links it
//	bench-whatsapp run      stay connected: record agent chats, serve the socket
//	bench-whatsapp unlink   log this device out and delete its session
//
// Everything lives in ~/Library/Application Support/agent-runner/whatsapp/:
// session.db (keys and state), agents.json, inbox/messages.jsonl,
// inbox/attachments/, state.json, status.json and bench.sock, all owner-only.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func defaultDir() string {
	if dir := os.Getenv("BENCH_WHATSAPP_DIR"); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "Application Support", "agent-runner", "whatsapp")
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: bench-whatsapp link | run | unlink")
		os.Exit(64)
	}
	dir := defaultDir()
	var err error
	switch os.Args[1] {
	case "link":
		err = link(dir)
	case "run":
		err = run(dir)
	case "unlink":
		err = unlink(dir)
	default:
		fmt.Fprintln(os.Stderr, "usage: bench-whatsapp link | run | unlink")
		os.Exit(64)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "bench-whatsapp:", err)
		os.Exit(1)
	}
}

// lock makes sure only one bench-whatsapp uses the session at a time.
func lock(dir string) (*os.File, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	_ = os.Chmod(dir, 0o700)
	f, err := os.OpenFile(filepath.Join(dir, "bench.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, errors.New("another bench-whatsapp is using the session (the background bridge, or a link in progress)")
	}
	return f, nil
}

func logger() waLog.Logger {
	if os.Getenv("BENCH_WHATSAPP_DEBUG") != "" {
		return waLog.Stdout("whatsmeow", "WARN", false)
	}
	return waLog.Noop // whatsmeow's own logs can name other contacts, so they're off by default
}

func openDevice(ctx context.Context, dir string) (*store.Device, error) {
	db := filepath.Join(dir, "session.db")
	container, err := sqlstore.New(ctx, "sqlite3", "file:"+(&url.URL{Path: db}).EscapedPath()+"?_foreign_keys=on", logger())
	if err != nil {
		return nil, err
	}
	_ = os.Chmod(db, 0o600)
	return container.GetFirstDevice(ctx)
}

func newClient(device *store.Device) *whatsmeow.Client {
	// Ask for as little history as WhatsApp allows, and then don't download it anyway.
	store.DeviceProps.Os = proto.String("agent-runner")
	store.DeviceProps.RequireFullSync = proto.Bool(false)
	store.DeviceProps.HistorySyncConfig = &waCompanionReg.DeviceProps_HistorySyncConfig{
		FullSyncDaysLimit:             proto.Uint32(1),
		FullSyncSizeMbLimit:           proto.Uint32(1),
		StorageQuotaMb:                proto.Uint32(1),
		RecentSyncDaysLimit:           proto.Uint32(1),
		InitialSyncMaxMessagesPerChat: proto.Uint32(1),
		InlineInitialPayloadInE2EeMsg: proto.Bool(false),
	}
	client := whatsmeow.NewClient(device, logger())
	client.ManualHistorySyncDownload = true
	return client
}

func printJSON(v any) {
	data, _ := json.Marshal(v)
	fmt.Println(string(data))
}

func waitConnected(client *whatsmeow.Client, timeout time.Duration) {
	connected := make(chan struct{}, 1)
	id := client.AddEventHandler(func(evt any) {
		if _, ok := evt.(*events.Connected); ok {
			select {
			case connected <- struct{}{}:
			default:
			}
		}
	})
	defer client.RemoveEventHandler(id)
	if client.IsConnected() && client.IsLoggedIn() {
		return
	}
	select {
	case <-connected:
	case <-time.After(timeout):
	}
}

func link(dir string) error {
	f, err := lock(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	ctx := context.Background()
	device, err := openDevice(ctx, dir)
	if err != nil {
		return err
	}
	if device.ID != nil {
		printJSON(map[string]string{"event": "already_linked", "phone": "+" + device.ID.User})
		return nil
	}
	client := newClient(device)
	qr, err := client.GetQRChannel(ctx)
	if err != nil {
		return err
	}
	if err := client.Connect(); err != nil {
		return err
	}
	defer client.Disconnect()
	for item := range qr {
		switch item.Event {
		case whatsmeow.QRChannelEventCode:
			printJSON(map[string]any{"event": "code", "code": item.Code, "seconds": item.Timeout.Seconds()})
		case whatsmeow.QRChannelSuccess.Event:
			// Let the new device finish its first connection before stopping.
			waitConnected(client, 30*time.Second)
			time.Sleep(5 * time.Second)
			printJSON(map[string]string{"event": "success", "phone": "+" + client.Store.ID.User})
			return nil
		case whatsmeow.QRChannelTimeout.Event:
			printJSON(map[string]string{"event": "timeout"})
			return errors.New("no QR code was scanned in time; run it again")
		default:
			if item.Error != nil {
				printJSON(map[string]string{"event": "error", "error": item.Error.Error()})
				return item.Error
			}
			printJSON(map[string]string{"event": item.Event})
		}
	}
	return errors.New("linking stopped before it finished")
}

func notify(message string) {
	script := fmt.Sprintf("display notification %q with title %q", message, "agent-runner")
	_ = exec.Command("osascript", "-e", script).Run()
}

func run(dir string) error {
	f, err := lock(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	device, err := openDevice(ctx, dir)
	if err != nil {
		return err
	}
	client := newClient(device)
	bridge := newBridge(dir, client, client.Store.LIDs.GetPNForLID)
	if device.ID == nil {
		bridge.setStatus("not_linked")
		bridge.logf("not linked to WhatsApp yet; run: uv run bench whatsapp-link")
		return nil // a clean exit, so launchd doesn't keep restarting it
	}
	loggedOut := make(chan struct{})
	client.AddEventHandler(func(evt any) {
		bridge.handle(ctx, evt)
		if _, ok := evt.(*events.LoggedOut); ok {
			notify("WhatsApp unlinked agent-runner. Run `uv run bench whatsapp-link` to link it again.")
			close(loggedOut)
		}
	})
	if err := client.Connect(); err != nil {
		return err
	}
	defer client.Disconnect()
	socket := filepath.Join(dir, "bench.sock")
	go func() {
		if err := bridge.serve(ctx, socket); err != nil {
			bridge.logf("socket stopped: %v", err)
			cancel()
		}
	}()
	defer os.Remove(socket)
	bridge.logf("running for %d agents", len(bridge.currentAgents()))
	select {
	case <-ctx.Done():
		_ = bridge.SetAvailable(context.Background(), false)
		bridge.setStatus("stopped")
	case <-loggedOut:
	}
	return nil
}

func unlink(dir string) error {
	f, err := lock(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	ctx := context.Background()
	device, err := openDevice(ctx, dir)
	if err != nil {
		return err
	}
	if device.ID != nil {
		client := newClient(device)
		if err := client.Connect(); err != nil {
			return err
		}
		waitConnected(client, 20*time.Second)
		if err := client.Logout(ctx); err != nil {
			client.Disconnect()
			return fmt.Errorf("couldn't log out (remove it on your phone under Linked Devices instead): %w", err)
		}
	}
	for _, name := range []string{"session.db", "session.db-wal", "session.db-shm", "state.json", "status.json"} {
		_ = os.Remove(filepath.Join(dir, name))
	}
	printJSON(map[string]string{"event": "unlinked"})
	return nil
}
