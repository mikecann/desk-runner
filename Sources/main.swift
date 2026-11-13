// Desk Runner: a background app that holds Screen Recording and Accessibility
// permission, and runs shell commands it's sent as its own children so they
// inherit that permission. Commands come in over a Unix socket that only this
// user can open, so it gives nothing beyond what a shell login already has.

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

#if DESK_RUNNER_TESTING
// The transport tests run a separate server, without TCC prompts or the live socket.
let supportDir = URL(fileURLWithPath: ProcessInfo.processInfo.environment["DESK_RUNNER_TEST_DIR"]!)
#else
let supportDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Desk Runner")
#endif
let socketPath = supportDir.appendingPathComponent("runner.sock").path
let rcMarker = "\u{0}DESK_RC="
#if DESK_RUNNER_TESTING
let sendTimeoutSeconds = 3
#else
let sendTimeoutSeconds = 30
#endif

func permissions() -> String {
    let screen = CGPreflightScreenCaptureAccess() ? "yes" : "no"
    let ax = AXIsProcessTrusted() ? "yes" : "no"
    return "screen-recording: \(screen)\naccessibility: \(ax)\n"
}

final class Connection {
    let fd: Int32
    let id: String
    private var closed = false
    private var disconnected = false
    private var timedOut = false
    private var discarded = 0

    init(_ fd: Int32, id: String) {
        self.fd = fd
        self.id = id
    }

    func send(_ data: Data) {
        guard !closed else { return }
        guard !disconnected, !timedOut else { discarded += data.count; return }
        writeAll(data)
    }

    func finish(_ data: Data = Data(), status: Int32? = nil) {
        guard !closed else { return }
        if !disconnected {
            if timedOut {
                timedOut = false
                let result = status.map { " Command exit status: \($0)." } ?? ""
                let notice = "\nDesk Runner: \(discarded) bytes of output were not delivered because the reader stopped accepting output.\(result) (request \(id)).\n"
                writeAll(Data("\(notice)\(rcMarker)125\n".utf8))
            } else {
                writeAll(data)
            }
        }
        closed = true
        close(fd)
    }

    private func writeAll(_ data: Data) {
        data.withUnsafeBytes { raw in
            guard var p = raw.baseAddress else { return }
            var left = raw.count
            let deadline = ProcessInfo.processInfo.systemUptime + Double(sendTimeoutSeconds)
            while left > 0 {
                // A timed-out write may return a partial count. Don't give its
                // remaining bytes another full timeout if the reader is stuck.
                if ProcessInfo.processInfo.systemUptime >= deadline {
                    timedOut = true
                    discarded += left
                    NSLog("request=%@ output timed out; command is not cancelled", id)
                    return
                }
                let n = write(fd, p, left)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 {
                    let code = n < 0 ? errno : EIO
                    discarded += left
                    if code == EAGAIN || code == EWOULDBLOCK {
                        timedOut = true
                        NSLog("request=%@ output timed out; command is not cancelled", id)
                    } else {
                        disconnected = true
                        NSLog("request=%@ client disconnected while sending (errno=%d); command is not cancelled", id, code)
                    }
                    return
                }
                p += n
                left -= n
            }
        }
    }
}

// Keep background descendants' stdout open after returning the parent's status.
// Closing it would kill them with SIGPIPE the next time they wrote a log line.
func discardBackgroundOutput(_ reader: FileHandle) {
    let fd = reader.fileDescriptor
    var buffer = [UInt8](repeating: 0, count: 65536)
    if read(fd, &buffer, buffer.count) == 0 {
        reader.closeFile()
        return
    }
    Thread.detachNewThread {
        defer { reader.closeFile() }
        var buffer = [UInt8](repeating: 0, count: 65536)
        while true {
            let count = read(fd, &buffer, buffer.count)
            if count == 0 { return }
            if count < 0 {
                if errno != EAGAIN && errno != EINTR { return }
                var event = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
                _ = poll(&event, 1, 1000)
            }
        }
    }
}

func readRequest(_ fd: Int32) -> [String: Any]? {
    var buffer = Data()
    var byte: UInt8 = 0
    while read(fd, &byte, 1) == 1 {
        if byte == 10 { break }
        buffer.append(byte)
        if buffer.count > 4_000_000 { return nil }
    }
    return (try? JSONSerialization.jsonObject(with: buffer)) as? [String: Any]
}

func handle(_ fd: Int32) {
    // Suppress SIGPIPE for this socket only. Ignoring it process-wide would
    // also change the signal disposition inherited by commands we launch.
    var enabled: Int32 = 1
    guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &enabled, socklen_t(MemoryLayout<Int32>.size)) == 0 else {
        NSLog("could not set SO_NOSIGPIPE: errno=%d", errno)
        close(fd)
        return
    }
    // A connected caller that stops reading must not leave its command blocked
    // behind a full output pipe forever. On timeout we discard further output
    // but continue draining and waiting for that command.
    var timeout = timeval(tv_sec: sendTimeoutSeconds, tv_usec: 0)
    guard setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else {
        NSLog("could not set SO_SNDTIMEO: errno=%d", errno)
        close(fd)
        return
    }
    let request = readRequest(fd)
    let suppliedID = request?["id"] as? String ?? ""
    let id = !suppliedID.isEmpty && suppliedID.utf8.count <= 64 && suppliedID.utf8.allSatisfy({
        (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45
    }) ? suppliedID : UUID().uuidString
    let connection = Connection(fd, id: id)
    defer { connection.finish() }
    guard let request, let command = request["cmd"] as? String else {
        connection.send(Data("bad request\n\(rcMarker)2\n".utf8))
        return
    }
    if command == "__permissions" {
        connection.send(Data("\(permissions())\(rcMarker)0\n".utf8))
        return
    }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/bin/bash")
    process.arguments = ["-c", command]
    if let cwd = request["cwd"] as? String, FileManager.default.fileExists(atPath: cwd) {
        process.currentDirectoryURL = URL(fileURLWithPath: cwd)
    }
    var env = ProcessInfo.processInfo.environment
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    env["PATH"] = "\(home)/.local/bin:\(home)/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    process.environment = env
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = pipe
    process.standardInput = FileHandle.nullDevice

    let reader = pipe.fileHandleForReading
    let pipeFD = reader.fileDescriptor
    let flags = fcntl(pipeFD, F_GETFL)
    guard flags >= 0, fcntl(pipeFD, F_SETFL, flags | O_NONBLOCK) == 0 else {
        NSLog("request=%@ could not configure output pipe: errno=%d", id, errno)
        connection.finish(Data("Desk Runner: could not configure command output.\n\(rcMarker)127\n".utf8))
        return
    }
    let terminated = DispatchSemaphore(value: 0)
    process.terminationHandler = { _ in terminated.signal() }
    do {
        try process.run()
    } catch {
        NSLog("request=%@ could not start: %@", id, String(describing: error))
        connection.send(Data("could not start: \(error)\n\(rcMarker)127\n".utf8))
        return
    }
    NSLog("request=%@ child=%d started", id, process.processIdentifier)
    // One thread owns both the pipe and socket. A slow client must not let an
    // exit marker overtake output already written by the command.
    var outputFailed = false
    var buffer = [UInt8](repeating: 0, count: 65536)
    while process.isRunning {
        let count = read(pipeFD, &buffer, buffer.count)
        if count > 0 {
            connection.send(Data(buffer.prefix(count)))
        } else if count == 0 {
            break
        } else if errno == EAGAIN || errno == EINTR {
            var event = pollfd(fd: pipeFD, events: Int16(POLLIN), revents: 0)
            _ = poll(&event, 1, 100)
        } else {
            NSLog("request=%@ output pipe read failed: errno=%d", id, errno)
            outputFailed = true
            break
        }
    }
    terminated.wait()
    // The child has finished writing. Drain the bytes currently buffered, then
    // finish even if a background descendant still has the pipe open.
    var remaining: Int32 = 0
    // Darwin's FIONREAD is _IOR('f', 127, int); Swift cannot import that C macro.
    let fionread: UInt = 0x4004667f
    if ioctl(pipeFD, fionread, &remaining) == 0 {
        while remaining > 0 {
            let count = read(pipeFD, &buffer, min(buffer.count, Int(remaining)))
            if count < 0 && errno == EINTR { continue }
            if count <= 0 { outputFailed = true; break }
            connection.send(Data(buffer.prefix(count)))
            remaining -= Int32(count)
        }
    } else {
        outputFailed = true
        NSLog("request=%@ could not drain output pipe: errno=%d", id, errno)
    }
    // Shell process substitutions (for example `exec > >(tee log)`) can flush
    // their last bytes after bash exits. Give them a short quiet period before
    // treating the pipe as output from a long-lived background job.
    let graceLimit = ProcessInfo.processInfo.systemUptime + 2
    var lastOutput = ProcessInfo.processInfo.systemUptime
    while ProcessInfo.processInfo.systemUptime < graceLimit &&
          ProcessInfo.processInfo.systemUptime - lastOutput < 0.5 {
        let count = read(pipeFD, &buffer, buffer.count)
        if count > 0 {
            connection.send(Data(buffer.prefix(count)))
            lastOutput = ProcessInfo.processInfo.systemUptime
        } else if count == 0 {
            break
        } else if errno == EAGAIN || errno == EINTR {
            var event = pollfd(fd: pipeFD, events: Int16(POLLIN), revents: 0)
            _ = poll(&event, 1, 50)
        } else {
            outputFailed = true
            break
        }
    }
    discardBackgroundOutput(reader)
    if outputFailed {
        connection.finish(Data("Desk Runner: command output was lost (request \(id)); result unknown.\n\(rcMarker)125\n".utf8))
        return
    }
    let signalled = process.terminationReason == .uncaughtSignal
    let status = process.terminationStatus + (signalled ? 128 : 0)
    NSLog("request=%@ child=%d finished status=%d signal=%d", id, process.processIdentifier, status,
          signalled ? process.terminationStatus : 0)
    let diagnostic = signalled ? "Desk Runner: command terminated by signal \(process.terminationStatus) (request \(id)).\n" : ""
    connection.finish(Data("\(diagnostic)\(rcMarker)\(status)\n".utf8), status: status)
}

func serve() {
    try? FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true,
                                             attributes: [.posixPermissions: 0o700])
    unlink(socketPath)
    let server = socket(AF_UNIX, SOCK_STREAM, 0)
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    _ = withUnsafeMutablePointer(to: &address.sun_path) { pointer in
        socketPath.withCString { strncpy(UnsafeMutableRawPointer(pointer).assumingMemoryBound(to: CChar.self), $0, 103) }
    }
    let size = socklen_t(MemoryLayout<sockaddr_un>.size)
    let bound = withUnsafePointer(to: &address) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(server, $0, size) }
    }
    guard bound == 0 else { fatalError("bind failed: \(errno)") }
    chmod(socketPath, 0o600)
    listen(server, 16)
    NSLog("listening at %@", socketPath)
    while true {
        let client = accept(server, nil, nil)
        if client < 0 { continue }
        Thread.detachNewThread { handle(client) }
    }
}

#if DESK_RUNNER_TESTING
serve()
#else
// Ask once, so Desk Runner shows up in both lists in System Settings.
_ = CGRequestScreenCaptureAccess()
_ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)

Thread.detachNewThread { serve() }
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
#endif
