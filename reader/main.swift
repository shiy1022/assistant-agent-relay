// bench-reader: the only part of agent-runner with Full Disk Access.
//
// Every few seconds it reads ~/Library/Messages/chat.db (read-only) and appends
// messages from the 1:1 chats with the agents in `allowlist` to
// <out>/messages.jsonl, copying their attachments to <out>/attachments/.
// The allowlist is compiled in (Allowlist.swift, generated from
// config/agents.yaml) and nothing it accepts at run time can name a chat or a
// query, so it cannot read any other conversation.
//
// Output is at-least-once: each line is a "new" or "update" event for one
// message. Updates carry the whole message again (after an edit, a delivery
// or read receipt, or an attachment finishing its download); for each guid
// the latest event wins.

import Foundation
import SQLite3

let appleEpoch: TimeInterval = 978_307_200  // 2001-01-01 in Unix time
let trackWindow: TimeInterval = 48 * 3600   // how long to keep watching a message for changes
let sqliteTransient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

// MARK: - Options

struct Options {
    var out = URL(fileURLWithPath: "/")
    var interval: TimeInterval = 2
    var once = false
    var dbPath = NSHomeDirectory() + "/Library/Messages/chat.db"
    var attachmentsRoot = NSHomeDirectory() + "/Library/Messages/Attachments"
}

func usage(_ message: String) -> Never {
    FileHandle.standardError.write(Data("""
        bench-reader: \(message)
        usage: bench-reader --out DIR [--interval SECONDS] [--once]

        """.utf8))
    exit(64)
}

func parseOptions(_ args: [String]) -> Options {
    var options = Options()
    var haveOut = false
    var rest = args.dropFirst().makeIterator()
    while let arg = rest.next() {
        func value() -> String {
            guard let v = rest.next() else { usage("\(arg) needs a value") }
            return v
        }
        switch arg {
        case "--out":
            options.out = URL(fileURLWithPath: value(), isDirectory: true)
            haveOut = true
        case "--interval":
            guard let seconds = Double(value()), seconds >= 0.5 else { usage("--interval must be at least 0.5 seconds") }
            options.interval = seconds
        case "--once":
            options.once = true
        #if TESTING
        // Test builds read a fake chat.db. Production builds have no way to
        // point the reader at another database or folder.
        case "--db":
            options.dbPath = value()
        case "--attachments-root":
            options.attachmentsRoot = value()
        #endif
        default:
            usage("unknown argument \(arg)")
        }
    }
    if !haveOut { usage("--out is required") }
    return options
}

// MARK: - Logging and dates

let logFormatter = ISO8601DateFormatter()

/// Logs go to launchd's log file. They never include message text.
func log(_ message: String) {
    FileHandle.standardError.write(Data("\(logFormatter.string(from: Date())) \(message)\n".utf8))
}

let isoFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

/// chat.db dates count from 2001-01-01: nanoseconds since macOS 10.13, seconds before. 0 means unset.
func appleSeconds(_ raw: Int64?) -> TimeInterval? {
    guard let raw, raw > 0 else { return nil }
    let seconds = raw > 1_000_000_000_000 ? Double(raw) / 1e9 : Double(raw)
    return seconds + appleEpoch
}

func appleDate(_ raw: Int64?) -> String? {
    appleSeconds(raw).map { isoFormatter.string(from: Date(timeIntervalSince1970: $0)) }
}

// MARK: - SQLite

struct DBError: Error, CustomStringConvertible {
    let description: String
}

enum Param {
    case int(Int64)
    case text(String)
}

struct Row {
    let stmt: OpaquePointer

    func int(_ i: Int32) -> Int64? {
        sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : sqlite3_column_int64(stmt, i)
    }

    func text(_ i: Int32) -> String? {
        sqlite3_column_text(stmt, i).map { String(cString: $0) }
    }

    func blob(_ i: Int32) -> Data? {
        guard let bytes = sqlite3_column_blob(stmt, i) else { return nil }
        return Data(bytes: bytes, count: Int(sqlite3_column_bytes(stmt, i)))
    }
}

final class Database {
    private let db: OpaquePointer

    init(readOnly path: String) throws {
        var handle: OpaquePointer?
        let rc = sqlite3_open_v2(path, &handle, SQLITE_OPEN_READONLY, nil)
        guard rc == SQLITE_OK, let handle else {
            let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "error \(rc)"
            sqlite3_close(handle)
            throw DBError(description: "can't open \(path): \(message)")
        }
        db = handle
        sqlite3_busy_timeout(db, 2000)
    }

    deinit {
        sqlite3_close(db)
    }

    func query(_ sql: String, _ params: [Param] = [], row: (Row) throws -> Void) throws {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { throw lastError() }
        defer { sqlite3_finalize(stmt) }
        for (i, param) in params.enumerated() {
            switch param {
            case .int(let v): sqlite3_bind_int64(stmt, Int32(i + 1), v)
            case .text(let v): sqlite3_bind_text(stmt, Int32(i + 1), v, -1, sqliteTransient)
            }
        }
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_DONE { return }
            guard rc == SQLITE_ROW else { throw lastError() }
            try row(Row(stmt: stmt))
        }
    }

    private func lastError() -> DBError {
        DBError(description: String(cString: sqlite3_errmsg(db)))
    }
}

// MARK: - chat.db

struct MessageRow {
    var rowid: Int64
    var guid: String
    var text: String?
    var body: Data?
    var date: Int64?
    var dateDelivered: Int64?
    var dateRead: Int64?
    var dateEdited: Int64?
    var dateRetracted: Int64?
    var isFromMe: Bool
    var service: String?
    var tapback: Int64?
    var tapbackTarget: String?
    var balloon: String?
    var hasAttachments: Bool
    var chat: String
}

struct AttachmentRow {
    var filename: String?
    var mimeType: String?
    var transferName: String?
    var totalBytes: Int64?
}

final class MessagesDB {
    private let db: Database
    private let messageColumns: Set<String>
    private let attachmentColumns: Set<String>

    init(path: String) throws {
        let db = try Database(readOnly: path)
        // Reading the schema is the first real read, so this is where a missing
        // Full Disk Access grant shows up.
        func columns(_ table: String) throws -> Set<String> {
            var names = Set<String>()
            try db.query("PRAGMA table_info(\(table))") { names.insert($0.text(1) ?? "") }
            return names
        }
        self.db = db
        do {
            messageColumns = try columns("message")
            attachmentColumns = try columns("attachment")
        } catch {
            throw DBError(description: "can't read \(path) (is Full Disk Access granted to bench-reader?): \(error)")
        }
        guard messageColumns.contains("guid") else { throw DBError(description: "\(path) has no message table") }
    }

    /// Columns differ between macOS versions; missing ones read as NULL.
    private func column(_ name: String, of columns: Set<String>, as alias: String) -> String {
        columns.contains(name) ? "\(alias).\(name)" : "NULL"
    }

    func maxRowID() throws -> Int64 {
        var max: Int64 = 0
        try db.query("SELECT IFNULL(MAX(ROWID), 0) FROM message") { max = $0.int(0) ?? 0 }
        return max
    }

    /// Lowercased chat identifiers among `handles` that have a chat on this Mac.
    func chatsPresent(_ handles: [String]) throws -> Set<String> {
        var found = Set<String>()
        let sql = "SELECT DISTINCT lower(chat_identifier) FROM chat WHERE lower(chat_identifier) IN (\(placeholders(handles.count)))"
        try db.query(sql, handles.map { .text($0) }) { row in
            if let identifier = row.text(0) { found.insert(identifier) }
        }
        return found
    }

    /// Messages after `rowid` in the 1:1 chats whose identifier is one of `handles`.
    /// Group chats have identifiers like "chat123…", so they never match.
    func agentMessages(after rowid: Int64, handles: [String]) throws -> [MessageRow] {
        func m(_ name: String) -> String { column(name, of: messageColumns, as: "m") }
        let sql = """
            SELECT m.ROWID, m.guid, m.text, \(m("attributedBody")), m.date, \(m("date_delivered")),
                   \(m("date_read")), \(m("date_edited")), \(m("date_retracted")), m.is_from_me,
                   \(m("service")), \(m("associated_message_type")), \(m("associated_message_guid")),
                   \(m("balloon_bundle_id")), \(m("cache_has_attachments")), c.chat_identifier
            FROM message m
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE m.ROWID > ? AND lower(c.chat_identifier) IN (\(placeholders(handles.count)))
            ORDER BY m.ROWID
            """
        var rows: [MessageRow] = []
        try db.query(sql, [.int(rowid)] + handles.map { .text($0) }) { r in
            rows.append(MessageRow(
                rowid: r.int(0) ?? 0, guid: r.text(1) ?? "", text: r.text(2), body: r.blob(3),
                date: r.int(4), dateDelivered: r.int(5), dateRead: r.int(6), dateEdited: r.int(7),
                dateRetracted: r.int(8), isFromMe: (r.int(9) ?? 0) != 0, service: r.text(10),
                tapback: r.int(11), tapbackTarget: r.text(12), balloon: r.text(13),
                hasAttachments: (r.int(14) ?? 0) != 0, chat: r.text(15) ?? ""))
        }
        return rows
    }

    func attachments(of messageRowID: Int64) throws -> [AttachmentRow] {
        func a(_ name: String) -> String { column(name, of: attachmentColumns, as: "a") }
        let sql = """
            SELECT \(a("filename")), \(a("mime_type")), \(a("transfer_name")), \(a("total_bytes"))
            FROM attachment a
            JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            WHERE maj.message_id = ?
            ORDER BY a.ROWID
            """
        var rows: [AttachmentRow] = []
        try db.query(sql, [.int(messageRowID)]) { r in
            rows.append(AttachmentRow(filename: r.text(0), mimeType: r.text(1), transferName: r.text(2), totalBytes: r.int(3)))
        }
        return rows
    }

    private func placeholders(_ count: Int) -> String {
        Array(repeating: "?", count: count).joined(separator: ",")
    }
}

// MARK: - attributedBody

/// Recent macOS often leaves `text` empty and stores the message in
/// `attributedBody`, an NSAttributedString in NeXTSTEP typedstream format.
/// This reads the text without creating any objects: the first NSString in
/// the stream is the message, stored after a "+" type tag and a length that
/// is one byte, or 0x81 plus 2 bytes, or 0x82 plus 4 bytes (little-endian).
func scanTypedStreamText(_ data: Data) -> String? {
    let bytes = [UInt8](data)
    let marker = Array("NSString".utf8)
    guard bytes.count > marker.count,
          let start = (0...(bytes.count - marker.count)).first(where: { bytes[$0..<$0 + marker.count].elementsEqual(marker) })
    else { return nil }
    var i = start + marker.count
    let tagLimit = min(i + 8, bytes.count)
    while i < tagLimit && bytes[i] != 0x2B { i += 1 }
    guard i + 1 < bytes.count, bytes[i] == 0x2B else { return nil }
    i += 1
    var length: Int
    switch bytes[i] {
    case 0x81:
        guard i + 2 < bytes.count else { return nil }
        length = Int(bytes[i + 1]) | Int(bytes[i + 2]) << 8
        i += 3
    case 0x82:
        guard i + 4 < bytes.count else { return nil }
        length = Int(bytes[i + 1]) | Int(bytes[i + 2]) << 8 | Int(bytes[i + 3]) << 16 | Int(bytes[i + 4]) << 24
        i += 5
    case let b where b < 0x80:
        length = Int(b)
        i += 1
    default:
        return nil
    }
    guard i + length <= bytes.count else { return nil }
    return String(bytes: bytes[i..<i + length], encoding: .utf8)
}

/// Apple's own decoder, for blobs the scanner doesn't understand.
func unarchiveText(_ data: Data) -> String? {
    (NSUnarchiver.unarchiveObject(with: data) as? NSAttributedString)?.string
}

// MARK: - Output

struct AttachmentOut: Codable {
    var name: String
    var mimeType: String?
    var bytes: Int64?
    var path: String?  // relative to --out; absent until the file has downloaded and been copied
}

struct Event: Codable {
    var kind = ""       // "new" or "update"
    var emittedAt = ""
    var rowid: Int64
    var guid: String
    var agent: String
    var handle: String
    var service: String?
    var isFromMe: Bool
    var text: String?
    var date: String?
    var dateDelivered: String?
    var dateRead: String?
    var dateEdited: String?
    var dateRetracted: String?
    var tapback: Int64?        // associated_message_type: 2000–2006 adds a reaction, 3000–3006 removes it
    var tapbackTarget: String?
    var balloon: String?       // balloon_bundle_id, e.g. link previews and app messages
    var attachments: [AttachmentOut]
    var attributedBodyB64: String?
}

struct Tracked: Codable {
    var sig: String
    var date: TimeInterval
}

struct State: Codable {
    var lastRowID: Int64
    var tracked: [String: Tracked]  // message ROWID → signature of the last event written for it
}

func fnv1a(_ data: Data) -> String {
    var hash: UInt64 = 0xcbf2_9ce4_8422_2325
    for byte in data {
        hash ^= UInt64(byte)
        hash = hash &* 0x100_0000_01b3
    }
    return String(hash, radix: 16)
}

func safeName(_ s: String) -> String {
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
    var name = String(String.UnicodeScalarView(s.unicodeScalars.map { allowed.contains($0) ? $0 : "_" }))
    while name.hasPrefix(".") { name.removeFirst() }
    name = String(name.prefix(120))
    return name.isEmpty ? "attachment" : name
}

// MARK: - Reader

final class Reader {
    private let options: Options
    private let agentsByHandle: [String: String]
    private let handles: [String]
    private let fm = FileManager.default
    private let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return e
    }()
    private var db: MessagesDB?
    private var state: State?
    private var savedState: Data?
    private var skipUnarchive = Set<Int64>()
    private var lastError: (message: String, at: Date)?

    private var outputURL: URL { options.out.appendingPathComponent("messages.jsonl") }
    private var stateURL: URL { options.out.appendingPathComponent(".reader-state.json") }
    private var markerURL: URL { options.out.appendingPathComponent(".unarchiving") }

    init(options: Options) {
        self.options = options
        agentsByHandle = Dictionary(uniqueKeysWithValues: allowlist.map { ($0.handle.lowercased(), $0.agent) })
        handles = agentsByHandle.keys.sorted()
        try? fm.createDirectory(at: options.out, withIntermediateDirectories: true)
        if let data = try? Data(contentsOf: stateURL) {
            state = try? JSONDecoder().decode(State.self, from: data)
            savedState = data
        }
        // If the last run crashed inside NSUnarchiver, don't feed it that message again.
        if let text = try? String(contentsOf: markerURL, encoding: .utf8), let rowid = Int64(text) {
            log("message \(rowid) crashed the decoder last run; recording it without text")
            skipUnarchive.insert(rowid)
            try? fm.removeItem(at: markerURL)
        }
    }

    /// One pass over chat.db. Returns false if it couldn't be read.
    @discardableResult
    func tick() -> Bool {
        do {
            try poll(try openIfNeeded())
            return true
        } catch {
            db = nil
            let message = "\(error)"
            if lastError?.message != message || Date().timeIntervalSince(lastError!.at) > 60 {
                log(message)
                lastError = (message, Date())
            }
            return false
        }
    }

    private func openIfNeeded() throws -> MessagesDB {
        if let db { return db }
        let opened = try MessagesDB(path: options.dbPath)
        if state == nil {
            // First run: record only what arrives from now on.
            state = State(lastRowID: try opened.maxRowID(), tracked: [:])
            log("first run: starting after message \(state!.lastRowID)")
        }
        let present = try opened.chatsPresent(handles)
        let summary = allowlist
            .map { "\($0.agent) \(present.contains($0.handle.lowercased()) ? "chat found" : "no chat yet")" }
            .joined(separator: ", ")
        log("reading chat.db: \(summary)")
        lastError = nil
        db = opened
        return opened
    }

    private func poll(_ db: MessagesDB) throws {
        guard var state else { return }
        let oldestTracked = state.tracked.keys.compactMap { Int64($0) }.min()
        let floor = min(state.lastRowID, (oldestTracked ?? state.lastRowID + 1) - 1)
        var lines = Data()
        var newCount = 0, updateCount = 0
        var seen = Set<Int64>()
        for row in try db.agentMessages(after: floor, handles: handles) where seen.insert(row.rowid).inserted {
            let key = String(row.rowid)
            let isNew = row.rowid > state.lastRowID
            guard isNew || state.tracked[key] != nil else { continue }
            var event = makeEvent(row, db)
            let sig = fnv1a(try encoder.encode(event))
            if !isNew && state.tracked[key]?.sig == sig { continue }
            event.kind = isNew ? "new" : "update"
            event.emittedAt = isoFormatter.string(from: Date())
            lines.append(try encoder.encode(event))
            lines.append(0x0A)
            state.tracked[key] = Tracked(sig: sig, date: appleSeconds(row.date) ?? Date().timeIntervalSince1970)
            state.lastRowID = max(state.lastRowID, row.rowid)
            if isNew { newCount += 1 } else { updateCount += 1 }
        }
        let cutoff = Date().timeIntervalSince1970 - trackWindow
        state.tracked = state.tracked.filter { $0.value.date >= cutoff }
        if !lines.isEmpty {
            try append(lines)
            log("wrote \(newCount) new, \(updateCount) updated")
        }
        self.state = state
        try saveState(state)
    }

    private func makeEvent(_ row: MessageRow, _ db: MessagesDB) -> Event {
        var text = row.text
        if (text ?? "").isEmpty, let body = row.body {
            text = decodeBody(body, rowid: row.rowid) ?? text
        }
        let handle = row.chat.lowercased()
        return Event(
            rowid: row.rowid, guid: row.guid, agent: agentsByHandle[handle] ?? "unknown", handle: handle,
            service: row.service, isFromMe: row.isFromMe, text: text, date: appleDate(row.date),
            dateDelivered: appleDate(row.dateDelivered), dateRead: appleDate(row.dateRead),
            dateEdited: appleDate(row.dateEdited), dateRetracted: appleDate(row.dateRetracted),
            tapback: row.tapback == 0 ? nil : row.tapback, tapbackTarget: row.tapbackTarget, balloon: row.balloon,
            attachments: row.hasAttachments ? copyAttachments(row, db) : [],
            attributedBodyB64: row.body?.base64EncodedString())
    }

    private func decodeBody(_ body: Data, rowid: Int64) -> String? {
        #if TESTING
        switch ProcessInfo.processInfo.environment["BENCH_READER_DECODER"] {
        case "scanner": return scanTypedStreamText(body)
        case "unarchiver": return unarchiveText(body)
        default: break
        }
        #endif
        if let text = scanTypedStreamText(body) { return text }
        guard !skipUnarchive.contains(rowid) else { return nil }
        // NSUnarchiver raises (and crashes the process) on data it can't parse,
        // so leave a marker; the next run skips this message instead of crashing again.
        try? String(rowid).write(to: markerURL, atomically: true, encoding: .utf8)
        defer { try? fm.removeItem(at: markerURL) }
        return unarchiveText(body)
    }

    /// Copies a message's attachments into <out>/attachments/<guid>/. Only files
    /// under the Messages attachments folder are copied; one that hasn't
    /// downloaded yet is listed without a path and copied on a later pass.
    private func copyAttachments(_ row: MessageRow, _ db: MessagesDB) -> [AttachmentOut] {
        let rows: [AttachmentRow]
        do {
            rows = try db.attachments(of: row.rowid)
        } catch {
            log("attachments of message \(row.rowid): \(error)")
            return []
        }
        let root = URL(fileURLWithPath: options.attachmentsRoot).standardizedFileURL.resolvingSymlinksInPath().path + "/"
        return rows.enumerated().map { index, attachment in
            let original = attachment.transferName ?? attachment.filename.map { ($0 as NSString).lastPathComponent } ?? "attachment"
            var out = AttachmentOut(name: original, mimeType: attachment.mimeType, bytes: attachment.totalBytes)
            guard let filename = attachment.filename else { return out }
            let source = URL(fileURLWithPath: (filename as NSString).expandingTildeInPath)
                .standardizedFileURL.resolvingSymlinksInPath()
            guard source.path.hasPrefix(root) else {
                log("message \(row.rowid): skipped an attachment outside the Messages attachments folder")
                return out
            }
            let relative = "attachments/\(safeName(row.guid))/\(index + 1)-\(safeName(original))"
            let dest = options.out.appendingPathComponent(relative)
            if !fm.fileExists(atPath: dest.path) {
                guard fm.fileExists(atPath: source.path) else { return out }
                do {
                    try fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try fm.copyItem(at: source, to: dest)
                } catch {
                    log("message \(row.rowid): copying an attachment failed: \(error.localizedDescription)")
                    return out
                }
            }
            out.path = relative
            return out
        }
    }

    private func append(_ data: Data) throws {
        if !fm.fileExists(atPath: outputURL.path) {
            fm.createFile(atPath: outputURL.path, contents: nil)
        }
        let file = try FileHandle(forWritingTo: outputURL)
        defer { try? file.close() }
        try file.seekToEnd()
        try file.write(contentsOf: data)
    }

    private func saveState(_ state: State) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = .sortedKeys  // stable bytes, so an unchanged state isn't rewritten
        let data = try encoder.encode(state)
        guard data != savedState else { return }
        try data.write(to: stateURL, options: .atomic)
        savedState = data
    }
}

// MARK: - Main

let options = parseOptions(CommandLine.arguments)
let reader = Reader(options: options)
if options.once {
    exit(reader.tick() ? 0 : 2)
}
log("started; agents: \(allowlist.map(\.agent).joined(separator: ", "))")
while true {
    reader.tick()
    Thread.sleep(forTimeInterval: options.interval)
}
