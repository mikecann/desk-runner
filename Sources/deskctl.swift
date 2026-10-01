// deskctl: mouse, keyboard and window helpers for scripted demos. Run it
// through `desk` so it has Accessibility permission.
//
//   deskctl move X Y [seconds]        glide the pointer there
//   deskctl click X Y [count]         glide there and click
//   deskctl rightclick X Y
//   deskctl drag X1 Y1 X2 Y2 [seconds]
//   deskctl type "text" [perKeySeconds]
//   deskctl key cmd+shift+4           press a combo (also: down, up, return, f11...)
//   deskctl hold KEY seconds          hold a key down
//   deskctl scroll DY [steps]
//   deskctl button N                  press an extra mouse button (3 = back, 4 = forward)
//   deskctl windows [app]             on-screen windows: id, app, title, x y w h
//   deskctl pos                       current pointer position

import AppKit
import CoreGraphics
import Foundation

let source = CGEventSource(stateID: .hidSystemState)

func pointer() -> CGPoint { CGEvent(source: nil)?.location ?? .zero }

func post(_ type: CGEventType, _ point: CGPoint, _ button: CGMouseButton = .left, clicks: Int64 = 1) {
    let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: button)
    event?.setIntegerValueField(.mouseEventClickState, value: clicks)
    event?.post(tap: .cghidEventTap)
}

func glide(to target: CGPoint, seconds: Double, dragging: Bool = false) {
    let start = pointer()
    let steps = max(1, Int(seconds * 60))
    for i in 1...steps {
        let t = Double(i) / Double(steps)
        let ease = t < 0.5 ? 2 * t * t : 1 - pow(-2 * t + 2, 2) / 2
        let p = CGPoint(x: start.x + (target.x - start.x) * ease, y: start.y + (target.y - start.y) * ease)
        post(dragging ? .leftMouseDragged : .mouseMoved, p)
        usleep(useconds_t(seconds / Double(steps) * 1_000_000))
    }
}

let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103,
    "f10": 109, "f12": 111, "f4": 118, "f2": 120, "f1": 122, "left": 123, "right": 124, "down": 125,
    "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
]

// Some apps (pynput hotkeys, for one) ignore key events that carry the sending
// process's ID, since that marks them as synthetic. Demos want the app to react
// as it would to a real key press.
func asIfTyped(_ event: CGEvent?) {
    event?.setIntegerValueField(.eventSourceUnixProcessID, value: 0)
}

func key(_ combo: String, hold: Double = 0) {
    var flags: CGEventFlags = []
    var name = ""
    for part in combo.lowercased().split(separator: "+").map(String.init) {
        switch part {
        case "cmd", "command": flags.insert(.maskCommand)
        case "shift": flags.insert(.maskShift)
        case "alt", "option", "opt": flags.insert(.maskAlternate)
        case "ctrl", "control": flags.insert(.maskControl)
        default: name = part
        }
    }
    guard let code = keyCodes[name] else { fatalError("unknown key: \(name)") }
    let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
    down?.flags = flags
    asIfTyped(down)
    down?.post(tap: .cghidEventTap)
    usleep(useconds_t(max(hold, 0.03) * 1_000_000))
    let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
    up?.flags = flags
    asIfTyped(up)
    up?.post(tap: .cghidEventTap)
    usleep(30_000)
}

let shifted: [Character: String] = [
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`",
]

// Press real keys where there is one, so every app sees ordinary typing.
// Anything else (accents, emoji) is sent as a Unicode string.
func type(_ text: String, perKey: Double) {
    for character in text {
        let lower = String(character).lowercased()
        if character == "\n" {
            key("return")
        } else if character == " " {
            key("space")
        } else if let base = shifted[character] {
            key("shift+\(base)")
        } else if keyCodes[lower] != nil, lower.count == 1 {
            key(character.isUppercase ? "shift+\(lower)" : lower)
        } else {
            var units = Array(String(character).utf16)
            for down in [true, false] {
                let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down)
                event?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
                asIfTyped(event)
                event?.post(tap: .cghidEventTap)
            }
        }
        usleep(useconds_t(perKey * 1_000_000))
    }
}

func windows(app filter: String?) {
    let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    for info in list {
        guard (info[kCGWindowLayer as String] as? Int) == 0 else { continue }
        let owner = info[kCGWindowOwnerName as String] as? String ?? ""
        if let filter, !owner.localizedCaseInsensitiveContains(filter) { continue }
        let b = info[kCGWindowBounds as String] as? [String: Double] ?? [:]
        let title = info[kCGWindowName as String] as? String ?? ""
        print("\(info[kCGWindowNumber as String] ?? 0)\t\(owner)\t\(title)\t\(Int(b["X"] ?? 0)) \(Int(b["Y"] ?? 0)) \(Int(b["Width"] ?? 0)) \(Int(b["Height"] ?? 0))")
    }
}

var args = Array(CommandLine.arguments.dropFirst())
guard !args.isEmpty else { print("usage: deskctl move|click|rightclick|drag|type|key|hold|scroll|windows|pos ..."); exit(2) }
let command = args.removeFirst()
let n = args.compactMap(Double.init)

switch command {
case "move":
    glide(to: CGPoint(x: n[0], y: n[1]), seconds: n.count > 2 ? n[2] : 0.6)
case "click", "rightclick":
    let p = CGPoint(x: n[0], y: n[1])
    glide(to: p, seconds: 0.6)
    let count = command == "click" && n.count > 2 ? Int64(n[2]) : 1
    let right = command == "rightclick"
    for i in 1...count {
        post(right ? .rightMouseDown : .leftMouseDown, p, right ? .right : .left, clicks: i)
        usleep(60_000)
        post(right ? .rightMouseUp : .leftMouseUp, p, right ? .right : .left, clicks: i)
        usleep(80_000)
    }
case "drag":
    glide(to: CGPoint(x: n[0], y: n[1]), seconds: 0.5)
    post(.leftMouseDown, CGPoint(x: n[0], y: n[1]))
    usleep(120_000)
    glide(to: CGPoint(x: n[2], y: n[3]), seconds: n.count > 4 ? n[4] : 0.8, dragging: true)
    usleep(120_000)
    post(.leftMouseUp, CGPoint(x: n[2], y: n[3]))
case "type":
    type(args[0], perKey: args.count > 1 ? Double(args[1]) ?? 0.04 : 0.04)
case "key":
    key(args[0])
case "hold":
    key(args[0], hold: Double(args[1]) ?? 1)
case "scroll":
    let steps = n.count > 1 ? Int(n[1]) : 10
    for _ in 0..<steps {
        CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 1, wheel1: Int32(n[0]) / Int32(steps), wheel2: 0, wheel3: 0)?.post(tap: .cghidEventTap)
        usleep(16_000)
    }
case "button":
    let p = pointer()
    for type in [CGEventType.otherMouseDown, .otherMouseUp] {
        let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p, mouseButton: .center)
        event?.setIntegerValueField(.mouseEventButtonNumber, value: Int64(n[0]))
        event?.post(tap: .cghidEventTap)
        usleep(80_000)
    }
case "windows":
    windows(app: args.first)
case "pos":
    let p = pointer()
    print("\(Int(p.x)) \(Int(p.y))")
default:
    print("unknown command \(command)")
    exit(2)
}
