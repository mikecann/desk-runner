// Desk Runner: a background app that holds Screen Recording and Accessibility
// permission, and runs shell commands it's sent as its own children so they
// inherit that permission. Commands come in over a Unix socket that only this
// user can open, so it gives nothing beyond what a shell login already has.

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

let supportDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/Desk Runner")
let socketPath = supportDir.appendingPathComponent("runner.sock").path
let rcMarker = "\u{0}DESK_RC="

func permissions() -> String {
    let screen = CGPreflightScreenCaptureAccess() ? "yes" : "no"
    let ax = AXIsProcessTrusted() ? "yes" : "no"
    return "screen-recording: \(screen)\naccessibility: \(ax)\n"
}

func writeAll(_ fd: Int32, _ data: Data) {
    data.withUnsafeBytes { raw in
        guard var p = raw.baseAddress else { return }
        var left = raw.count
        while left > 0 {
            let n = write(fd, p, left)
            if n <= 0 { return }
            p += n
            left -= n
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
    defer { close(fd) }
    guard let request = readRequest(fd), let command = request["cmd"] as? String else {
        writeAll(fd, Data("bad request\n\(rcMarker)2\n".utf8))
        return
    }
    if command == "__permissions" {
        writeAll(fd, Data("\(permissions())\(rcMarker)0\n".utf8))
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

    do {
        try process.run()
    } catch {
        writeAll(fd, Data("could not start: \(error)\n\(rcMarker)127\n".utf8))
        return
    }
    // Stream output until the command exits. Anything it left running in the
    // background may hold the pipe open forever, so don't wait for EOF.
    let reader = pipe.fileHandleForReading
    let drained = DispatchSemaphore(value: 0)
    reader.readabilityHandler = { handle in
        let chunk = handle.availableData
        if chunk.isEmpty {
            handle.readabilityHandler = nil
            drained.signal()
        } else {
            writeAll(fd, chunk)
        }
    }
    process.waitUntilExit()
    _ = drained.wait(timeout: .now() + 0.5)
    reader.readabilityHandler = nil
    writeAll(fd, Data("\(rcMarker)\(process.terminationStatus)\n".utf8))
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
    while true {
        let client = accept(server, nil, nil)
        if client < 0 { continue }
        Thread.detachNewThread { handle(client) }
    }
}

// Ask once, so Desk Runner shows up in both lists in System Settings.
_ = CGRequestScreenCaptureAccess()
_ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)

Thread.detachNewThread { serve() }
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.run()
