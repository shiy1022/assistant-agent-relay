package main

// What a WhatsApp message says: its text, buttons, link preview, reply
// target and media, pulled out of whatsmeow's protobuf message.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"mime"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// Message types whose content is fully captured by the fields we extract.
// Anything else also gets its raw form recorded, so nothing is lost.
var wellKnownKinds = map[string]bool{
	"conversation": true, "extendedTextMessage": true, "imageMessage": true, "videoMessage": true,
	"audioMessage": true, "documentMessage": true, "stickerMessage": true, "messageContextInfo": true,
}

// Field names that hold human-readable text, for message types we don't know.
var textFieldNames = map[string]bool{
	"conversation": true, "text": true, "messageText": true, "caption": true, "contentText": true,
	"description": true, "title": true, "displayText": true, "hydratedContentText": true, "footerText": true,
}

func join(parts ...string) string {
	var kept []string
	for _, p := range parts {
		if strings.TrimSpace(p) != "" {
			kept = append(kept, p)
		}
	}
	return strings.Join(kept, "\n")
}

// messageText is the readable text of a message: plain text, captions, the
// body of buttons, lists and templates, and the text parts of Meta AI's rich
// responses. Other types fall back to every text-like field they contain.
func messageText(m *waE2E.Message) string {
	switch {
	case m == nil:
		return ""
	case m.GetConversation() != "":
		return m.GetConversation()
	case m.GetExtendedTextMessage() != nil:
		return m.GetExtendedTextMessage().GetText()
	case m.GetImageMessage() != nil:
		return m.GetImageMessage().GetCaption()
	case m.GetVideoMessage() != nil:
		return m.GetVideoMessage().GetCaption()
	case m.GetDocumentMessage() != nil:
		return m.GetDocumentMessage().GetCaption()
	case m.GetAudioMessage() != nil, m.GetStickerMessage() != nil:
		return ""
	case m.GetButtonsMessage() != nil:
		b := m.GetButtonsMessage()
		return join(b.GetContentText(), b.GetFooterText())
	case m.GetListMessage() != nil:
		l := m.GetListMessage()
		return join(l.GetTitle(), l.GetDescription(), l.GetFooterText())
	case m.GetTemplateMessage() != nil:
		t := m.GetTemplateMessage().GetHydratedTemplate()
		return join(t.GetHydratedTitleText(), t.GetHydratedContentText(), t.GetHydratedFooterText())
	case m.GetInteractiveMessage() != nil:
		i := m.GetInteractiveMessage()
		return join(i.GetHeader().GetTitle(), i.GetBody().GetText(), i.GetFooter().GetText())
	case m.GetRichResponseMessage() != nil:
		rich := m.GetRichResponseMessage()
		var parts []string
		for _, sub := range rich.GetSubmessages() {
			parts = append(parts, sub.GetMessageText())
		}
		if text := join(parts...); text != "" {
			return text
		}
		if text := unifiedText(rich.GetUnifiedResponse().GetData()); text != "" {
			return text
		}
	}
	return join(textFields(m.ProtoReflect(), 0)...)
}

// unifiedText reads Meta AI's unified response (what Muse sends): JSON whose
// "sections" hold UI primitives like {"__typename":
// "GenAIMarkdownTextUXPrimitive", "text": "…"}. Their texts are joined in order.
func unifiedText(data []byte) string {
	if len(data) == 0 {
		return ""
	}
	doc, err := decodeOrdered(json.NewDecoder(bytes.NewReader(data)))
	if err != nil {
		return ""
	}
	root, _ := doc.(*orderedObject)
	sections := root.get("sections")
	if sections == nil {
		sections = doc
	}
	var parts []string
	collectTexts(sections, &parts)
	return join(parts...)
}

// orderedObject keeps a JSON object's keys in order, so text comes out in reading order.
type orderedObject struct {
	keys   []string
	values []any
}

func (o *orderedObject) get(key string) any {
	if o == nil {
		return nil
	}
	for i, k := range o.keys {
		if k == key {
			return o.values[i]
		}
	}
	return nil
}

func decodeOrdered(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := tok.(json.Delim)
	if !ok {
		return tok, nil
	}
	switch delim {
	case '{':
		obj := &orderedObject{}
		for dec.More() {
			key, err := dec.Token()
			if err != nil {
				return nil, err
			}
			value, err := decodeOrdered(dec)
			if err != nil {
				return nil, err
			}
			obj.keys = append(obj.keys, key.(string))
			obj.values = append(obj.values, value)
		}
		_, err = dec.Token()
		return obj, err
	case '[':
		var list []any
		for dec.More() {
			value, err := decodeOrdered(dec)
			if err != nil {
				return nil, err
			}
			list = append(list, value)
		}
		_, err = dec.Token()
		return list, err
	}
	return nil, fmt.Errorf("unexpected %v", delim)
}

func collectTexts(v any, out *[]string) {
	switch x := v.(type) {
	case *orderedObject:
		typename, _ := x.get("__typename").(string)
		if text, ok := x.get("text").(string); ok && (typename == "" || strings.Contains(typename, "Text")) {
			*out = append(*out, text)
		}
		for i, key := range x.keys {
			if key != "text" {
				collectTexts(x.values[i], out)
			}
		}
	case []any:
		for _, item := range x {
			collectTexts(item, out)
		}
	}
}

func textFields(m protoreflect.Message, depth int) []string {
	if depth > 12 {
		return nil
	}
	var out []string
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		name := string(fd.Name())
		switch {
		case name == "contextInfo" || name == "messageContextInfo":
			// quoted messages and metadata, not this message's own words
		case fd.IsMap():
		case fd.Kind() == protoreflect.MessageKind && fd.IsList():
			for i := 0; i < v.List().Len(); i++ {
				out = append(out, textFields(v.List().Get(i).Message(), depth+1)...)
			}
		case fd.Kind() == protoreflect.MessageKind:
			out = append(out, textFields(v.Message(), depth+1)...)
		case fd.Kind() == protoreflect.StringKind && !fd.IsList() && textFieldNames[name]:
			out = append(out, v.String())
		}
		return true
	})
	return out
}

type button struct {
	Text string `json:"text"`
	Kind string `json:"kind"`
	URL  string `json:"url,omitempty"`
}

// messageButtons lists the buttons and list rows an agent offers, row by row.
func messageButtons(m *waE2E.Message) [][]button {
	var rows [][]button
	switch {
	case m.GetButtonsMessage() != nil:
		var row []button
		for _, b := range m.GetButtonsMessage().GetButtons() {
			row = append(row, button{Text: b.GetButtonText().GetDisplayText(), Kind: "reply"})
		}
		rows = append(rows, row)
	case m.GetListMessage() != nil:
		for _, section := range m.GetListMessage().GetSections() {
			var row []button
			for _, r := range section.GetRows() {
				row = append(row, button{Text: r.GetTitle(), Kind: "list_item"})
			}
			rows = append(rows, row)
		}
	case m.GetTemplateMessage() != nil:
		var row []button
		for _, b := range m.GetTemplateMessage().GetHydratedTemplate().GetHydratedButtons() {
			switch {
			case b.GetQuickReplyButton() != nil:
				row = append(row, button{Text: b.GetQuickReplyButton().GetDisplayText(), Kind: "reply"})
			case b.GetUrlButton() != nil:
				row = append(row, button{Text: b.GetUrlButton().GetDisplayText(), Kind: "url", URL: b.GetUrlButton().GetURL()})
			case b.GetCallButton() != nil:
				row = append(row, button{Text: b.GetCallButton().GetDisplayText(), Kind: "call"})
			}
		}
		rows = append(rows, row)
	case m.GetInteractiveMessage() != nil:
		var row []button
		for _, b := range m.GetInteractiveMessage().GetNativeFlowMessage().GetButtons() {
			var params struct {
				DisplayText string `json:"display_text"`
				URL         string `json:"url"`
			}
			_ = json.Unmarshal([]byte(b.GetButtonParamsJSON()), &params)
			row = append(row, button{Text: params.DisplayText, Kind: b.GetName(), URL: params.URL})
		}
		rows = append(rows, row)
	}
	var kept [][]button
	for _, row := range rows {
		if len(row) > 0 {
			kept = append(kept, row)
		}
	}
	return kept
}

func linkPreview(m *waE2E.Message) map[string]string {
	x := m.GetExtendedTextMessage()
	if x == nil || x.GetMatchedText() == "" {
		return nil
	}
	preview := map[string]string{"url": x.GetMatchedText()}
	if x.GetTitle() != "" {
		preview["title"] = x.GetTitle()
	}
	if x.GetDescription() != "" {
		preview["description"] = x.GetDescription()
	}
	return preview
}

// replyTo is the ID of the message this one quotes, if any.
func replyTo(m *waE2E.Message) string {
	found := ""
	m.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if fd.Kind() != protoreflect.MessageKind || fd.IsList() || fd.IsMap() {
			return true
		}
		if ctx := v.Message().Descriptor().Fields().ByName("contextInfo"); ctx != nil && v.Message().Has(ctx) {
			if info, ok := v.Message().Get(ctx).Message().Interface().(*waE2E.ContextInfo); ok && info.GetStanzaID() != "" {
				found = info.GetStanzaID()
				return false
			}
		}
		return true
	})
	return found
}

// kinds names the top-level parts of a message, like ["conversation"] or ["richResponseMessage"].
func kinds(m *waE2E.Message) []string {
	var names []string
	m.ProtoReflect().Range(func(fd protoreflect.FieldDescriptor, _ protoreflect.Value) bool {
		names = append(names, string(fd.Name()))
		return true
	})
	return names
}

// raw is the whole message as JSON, kept for types we don't fully understand.
func raw(m *waE2E.Message) json.RawMessage {
	for _, k := range kinds(m) {
		if !wellKnownKinds[k] {
			data, err := protojson.Marshal(m)
			if err != nil {
				return nil
			}
			return data
		}
	}
	return nil
}

type mediaInfo struct {
	part     whatsmeow.DownloadableMessage
	mimeType string
	name     string
	kind     string
}

func messageMedia(m *waE2E.Message) *mediaInfo {
	switch {
	case m.GetImageMessage() != nil:
		x := m.GetImageMessage()
		return &mediaInfo{x, x.GetMimetype(), "", "photo"}
	case m.GetVideoMessage() != nil:
		x := m.GetVideoMessage()
		return &mediaInfo{x, x.GetMimetype(), "", "video"}
	case m.GetAudioMessage() != nil:
		x := m.GetAudioMessage()
		kind := "audio"
		if x.GetPTT() {
			kind = "voice"
		}
		return &mediaInfo{x, x.GetMimetype(), "", kind}
	case m.GetDocumentMessage() != nil:
		x := m.GetDocumentMessage()
		return &mediaInfo{x, x.GetMimetype(), x.GetFileName(), "file"}
	case m.GetStickerMessage() != nil:
		x := m.GetStickerMessage()
		return &mediaInfo{x, x.GetMimetype(), "", "sticker"}
	}
	return nil
}

func (mi *mediaInfo) fileName() string {
	if mi.name != "" {
		return mi.name
	}
	base, _, _ := mime.ParseMediaType(mi.mimeType)
	ext := map[string]string{"audio/ogg": ".ogg", "image/jpeg": ".jpg", "image/webp": ".webp", "video/mp4": ".mp4"}[base]
	if ext == "" {
		if exts, _ := mime.ExtensionsByType(base); len(exts) > 0 {
			ext = exts[0]
		}
	}
	return mi.kind + ext
}
