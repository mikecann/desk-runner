// Desk Runner: a background app that holds Screen Recording and Accessibility
// permission, and runs shell commands it's sent as its own children so they
// inherit that permission. Commands come in over a Unix socket that only this
// user can open, so it gives nothing beyond what a shell login already has.

import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import os

// NSLog lines from a launchd agent didn't show up in `log show` on Tinker, and
// the README points people there for request IDs. Log them as public entries
// (no command text is ever logged), and also to stderr for the tests.
let logger = Logger(subsystem: "com.mikecann.desk-runner", category: "requests")
func note(_ format: String, _ args: CVarArg...) {
    let message = String(format: format, arguments: args)
    logger.log("\(message, privacy: .public)")
    #if DESK_RUNNER_TESTING
    FileHandle.standardError.write(Data((message + "\n").utf8))
    #endif
}

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
    private(set) var disconnected = false
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

    func markDisconnected() {
        if !disconnected { note("request=%@ client hung up", id) }
        disconnected = true
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
                    note("request=%@ output timed out; command is not cancelled", id)
                    return
                }
                let n = write(fd, p, left)
                if n < 0 && errno == EINTR { continue }
                if n <= 0 {
                    let code = n < 0 ? errno : EIO
                    discarded += left
                    if code == EAGAIN || code == EWOULDBLOCK {
                        timedOut = true
                        note("request=%@ output timed out; command is not cancelled", id)
                    } else {
                        disconnected = true
                        note("request=%@ client disconnected while sending (errno=%d)", id, code)
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

// posix_spawn rather than Process, so each command gets its own process group
// (one killpg stops it and its children) and inherits no other request's fds.
func spawn(_ args: [String], stdin: Int32, stdout: Int32, cwd: String?, env: [String: String]) -> pid_t? {
    var actions: posix_spawn_file_actions_t?
    var attr: posix_spawnattr_t?
    posix_spawn_file_actions_init(&actions)
    posix_spawnattr_init(&attr)
    defer {
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attr)
    }
    posix_spawn_file_actions_adddup2(&actions, stdin, 0)
    posix_spawn_file_actions_adddup2(&actions, stdout, 1)
    posix_spawn_file_actions_adddup2(&actions, stdout, 2)
    if let cwd { posix_spawn_file_actions_addchdir_np(&actions, cwd) }
    var none = sigset_t(), all = sigset_t()
    sigemptyset(&none)
    sigfillset(&all)
    posix_spawnattr_setsigmask(&attr, &none)
    posix_spawnattr_setsigdefault(&attr, &all)
    posix_spawnattr_setpgroup(&attr, 0)
    posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT |
                                          POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF))
    let argv = args.map { strdup($0) } + [nil]
    let envp = env.map { strdup("\($0.key)=\($0.value)") } + [nil]
    defer { (argv + envp).forEach { free($0) } }
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, args[0], &actions, &attr, argv, envp)
    if rc != 0 { errno = rc; return nil }
    return pid
}

// A request's command must not outlive its supervisor: if Desk Runner crashes,
// is restarted or is booted out mid-request, nobody can report the command's
// result, yet it could still move the real pointer minutes later. Each request
// gets a guard in its own process group (so launchd's cleanup of ours doesn't
// take it first). It reads the command's group ID, then waits: "release" means
// the command finished and later background jobs may carry on; end of input
// without it means Desk Runner went away or gave up on the request, so the
// guard stops the whole group, politely first.
let guardScript = """
IFS= read -r group || exit 0
IFS= read -r release && exit 0
kill -TERM -- "-$group" 2>/dev/null || exit 0
i=0
# About 2 s; each step spawns a sleep, so keep the steps few.
while [ $i -lt 10 ]; do kill -0 -- "-$group" 2>/dev/null || exit 0; sleep 0.2; i=$((i+1)); done
kill -KILL -- "-$group" 2>/dev/null
exit 0
"""

final class Lifeline {
    private var fd: Int32

    init?(id: String) {
        var fds: [Int32] = [0, 0]
        guard pipe(&fds) == 0 else { return nil }
        // The write end must reach no other child, or its EOF would never come.
        _ = fcntl(fds[1], F_SETFD, FD_CLOEXEC)
        _ = fcntl(fds[0], F_SETFD, FD_CLOEXEC)
        let null = open("/dev/null", O_WRONLY | O_CLOEXEC)
        let guardPID = spawn(["/bin/sh", "-c", guardScript], stdin: fds[0], stdout: null, cwd: nil,
                             env: ["PATH": "/usr/bin:/bin"])
        close(null)
        close(fds[0])
        guard let guardPID else { close(fds[1]); return nil }
        // Reap the guard whenever it exits, so it doesn't linger as a zombie.
        Thread.detachNewThread { var status: Int32 = 0; while waitpid(guardPID, &status, 0) < 0 && errno == EINTR {} }
        fd = fds[1]
        note("request=%@ guard=%d started", id, guardPID)
    }

    private func say(_ line: String) {
        guard fd >= 0 else { return }
        _ = line.withCString { write(fd, $0, strlen($0)) }
    }

    func watch(group: pid_t) { say("\(group)\n") }

    /// The command finished: leave anything it started in the background alone.
    func release() { say("release\n"); cut() }

    /// Stop the command's whole process group.
    func cut() {
        guard fd >= 0 else { return }
        close(fd)
        fd = -1
    }

    deinit { cut() }
}

func clientHungUp(_ fd: Int32) -> Bool {
    var event = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
    guard poll(&event, 1, 0) > 0 else { return false }
    if event.revents & Int16(POLLHUP | POLLERR | POLLNVAL) != 0 { return true }
    // The client sends nothing after its request, so readable means closed,
    // unless it sent stray bytes, which are discarded.
    var byte: UInt8 = 0
    let count = recv(fd, &byte, 1, MSG_DONTWAIT)
    return count == 0 || (count < 0 && errno != EAGAIN && errno != EINTR)
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
        note("could not set SO_NOSIGPIPE: errno=%d", errno)
        close(fd)
        return
    }
    // A connected caller that stops reading must not leave its command blocked
    // behind a full output pipe forever. On timeout we discard further output
    // but continue draining and waiting for that command.
    var timeout = timeval(tv_sec: sendTimeoutSeconds, tv_usec: 0)
    guard setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0 else {
        note("could not set SO_SNDTIMEO: errno=%d", errno)
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

    var env = ProcessInfo.processInfo.environment
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    env["PATH"] = "\(home)/.local/bin:\(home)/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    var cwd = request["cwd"] as? String
    if let dir = cwd, !FileManager.default.fileExists(atPath: dir) { cwd = nil }

    var fds: [Int32] = [0, 0]
    guard pipe(&fds) == 0 else {
        connection.finish(Data("Desk Runner: could not create command output.\n\(rcMarker)127\n".utf8))
        return
    }
    let pipeFD = fds[0]
    _ = fcntl(fds[0], F_SETFD, FD_CLOEXEC)
    _ = fcntl(fds[1], F_SETFD, FD_CLOEXEC)
    let reader = FileHandle(fileDescriptor: pipeFD, closeOnDealloc: false)
    let flags = fcntl(pipeFD, F_GETFL)
    guard flags >= 0, fcntl(pipeFD, F_SETFL, flags | O_NONBLOCK) == 0 else {
        note("request=%@ could not configure output pipe: errno=%d", id, errno)
        close(fds[0]); close(fds[1])
        connection.finish(Data("Desk Runner: could not configure command output.\n\(rcMarker)127\n".utf8))
        return
    }
    // Start the guard before the command, so there is no moment where the
    // command runs unguarded.
    guard let lifeline = Lifeline(id: id) else {
        note("request=%@ could not start guard: errno=%d", id, errno)
        close(fds[0]); close(fds[1])
        connection.finish(Data("Desk Runner: could not start the command's guard.\n\(rcMarker)127\n".utf8))
        return
    }
    let null = open("/dev/null", O_RDONLY | O_CLOEXEC)
    let spawned = spawn(["/bin/bash", "-c", command], stdin: null, stdout: fds[1], cwd: cwd, env: env)
    let spawnError = errno
    close(null)
    close(fds[1])
    guard let pid = spawned else {
        note("request=%@ could not start: errno=%d", id, spawnError)
        lifeline.cut()
        close(pipeFD)
        connection.send(Data("could not start: \(String(cString: strerror(spawnError)))\n\(rcMarker)127\n".utf8))
        return
    }
    // The command leads its own process group, so its ID is the group's.
    lifeline.watch(group: pid)
    note("request=%@ child=%d started", id, pid)

    var waitStatus: Int32 = 0
    let exited = DispatchSemaphore(value: 0)
    let lock = NSLock()
    var running = true
    Thread.detachNewThread {
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        lock.lock(); waitStatus = status; running = false; lock.unlock()
        exited.signal()
    }
    func isRunning() -> Bool { lock.lock(); defer { lock.unlock() }; return running }

    // One thread owns both the pipe and socket. A slow client must not let an
    // exit marker overtake output already written by the command.
    var outputFailed = false
    var cancelled = false
    var buffer = [UInt8](repeating: 0, count: 65536)
    while isRunning() {
        // Nobody is left to see the result or stop it, so stop the command.
        // A slow reader (timed out, still connected) doesn't count.
        if !cancelled && (connection.disconnected || clientHungUp(fd)) {
            connection.markDisconnected()
            note("request=%@ child=%d cancelled: client went away", id, pid)
            lifeline.cut()
            cancelled = true
        }
        let count = read(pipeFD, &buffer, buffer.count)
        if count > 0 {
            connection.send(Data(buffer.prefix(count)))
        } else if count == 0 {
            break
        } else if errno == EAGAIN || errno == EINTR {
            var events = [pollfd(fd: pipeFD, events: Int16(POLLIN), revents: 0),
                          pollfd(fd: fd, events: Int16(POLLIN), revents: 0)]
            _ = poll(&events, cancelled ? 1 : 2, 100)
        } else {
            note("request=%@ output pipe read failed: errno=%d", id, errno)
            outputFailed = true
            break
        }
    }
    // The pipe can close before the command ends (`exec >/dev/null`), so keep
    // watching for the client going away while waiting for the exit.
    while exited.wait(timeout: .now() + 0.1) == .timedOut {
        if !cancelled && (connection.disconnected || clientHungUp(fd)) {
            connection.markDisconnected()
            note("request=%@ child=%d cancelled: client went away", id, pid)
            lifeline.cut()
            cancelled = true
        }
    }
    // The command itself has finished. Anything it left in the background was
    // started on purpose, so let it carry on.
    if !cancelled { lifeline.release() }
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
        note("request=%@ could not drain output pipe: errno=%d", id, errno)
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
    if cancelled {
        connection.finish()
        return
    }
    if outputFailed {
        connection.finish(Data("Desk Runner: command output was lost (request \(id)); result unknown.\n\(rcMarker)125\n".utf8))
        return
    }
    // Swift can't import WIFSIGNALED and friends.
    let signal = waitStatus & 0x7f
    let signalled = signal != 0
    let status = signalled ? 128 + signal : (waitStatus >> 8) & 0xff
    note("request=%@ child=%d finished status=%d signal=%d", id, pid, status, signal)
    let diagnostic = signalled ? "Desk Runner: command terminated by signal \(signal) (request \(id)).\n" : ""
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
    note("listening at %@", socketPath)
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
