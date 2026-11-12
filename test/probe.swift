// Listen-only event tap: prints what other apps see for each key and mouse
// event, the text macOS attaches to key presses and the modifier flags.
// Used by keys.sh. Usage: probe SECONDS
import CoreGraphics
import Foundation

let seconds = Double(CommandLine.arguments.dropFirst().first ?? "5") ?? 5
let types: [CGEventType] = [.keyDown, .flagsChanged, .leftMouseDown, .leftMouseDragged, .leftMouseUp]
let mask = types.reduce(CGEventMask(0)) { $0 | (1 << $1.rawValue) }
func names(_ f: CGEventFlags) -> String {
    [(CGEventFlags.maskCommand, "cmd"), (.maskShift, "shift"), (.maskAlternate, "alt"), (.maskControl, "ctrl")]
        .filter { f.contains($0.0) }.map(\.1).joined(separator: "+")
}
let tap = CGEvent.tapCreate(tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
                            eventsOfInterest: mask, callback: { _, type, event, _ in
    switch type {
    case .keyDown:
        var len = 0
        var buf = [UniChar](repeating: 0, count: 8)
        event.keyboardGetUnicodeString(maxStringLength: 8, actualStringLength: &len, unicodeString: &buf)
        // Arrow and function keys carry private-use characters, so show those as codes.
        let text = String(utf16CodeUnits: buf, count: len).unicodeScalars
            .map { (32..<127).contains($0.value) ? String($0) : String(format: "U+%04X", $0.value) }.joined()
        print("key \(text) [\(names(event.flags))]")
    case .flagsChanged: print("flags [\(names(event.flags))]")
    case .leftMouseDown: print("mousedown [\(names(event.flags))]")
    case .leftMouseUp: print("mouseup [\(names(event.flags))]")
    default: break
    }
    fflush(stdout)
    return Unmanaged.passUnretained(event)
}, userInfo: nil)
guard let tap else { print("no tap (permission?)"); exit(1) }
CFRunLoopAddSource(CFRunLoopGetCurrent(), CFMachPortCreateRunLoopSource(nil, tap, 0), .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)
// keys.sh waits for this before it starts typing.
print("ready"); fflush(stdout)
CFRunLoopRunInMode(.defaultMode, seconds, false)
