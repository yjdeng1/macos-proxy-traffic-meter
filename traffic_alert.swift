import AppKit
import Foundation

struct Request: Decodable {
    let id: String
    let title: String
    let message: String
    let ackPath: String
}

final class AlertDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let request: Request
    let selfTest: Bool
    var window: NSWindow!
    var finished = false

    init(request: Request, selfTest: Bool) {
        self.request = request
        self.selfTest = selfTest
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 270),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = request.title
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let content = window.contentView!
        let heading = NSTextField(labelWithString: request.title)
        heading.font = .boldSystemFont(ofSize: 21)
        heading.frame = NSRect(x: 24, y: 211, width: 492, height: 30)
        content.addSubview(heading)
        let body = NSTextField(wrappingLabelWithString: request.message)
        body.font = .systemFont(ofSize: 14)
        body.frame = NSRect(x: 24, y: 70, width: 492, height: 133)
        content.addSubview(body)
        let button = NSButton(title: "我已知晓，关闭", target: self, action: #selector(closeWarning))
        button.bezelStyle = .rounded
        button.frame = NSRect(x: 350, y: 20, width: 166, height: 34)
        button.keyEquivalent = "\r"
        content.addSubview(button)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
            if let data = try? Data(contentsOf: URL(fileURLWithPath: self.request.ackPath)),
               let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               value["id"] as? String == self.request.id,
               value["acknowledged"] as? Bool == true {
                self.window.performClose(nil)
            }
        }
        if selfTest {
            let visible = window.isVisible
            let closeEnabled = window.standardWindowButton(.closeButton)?.isEnabled == true
            print("SELF_TEST visible=\(visible) closeEnabled=\(closeEnabled)")
            Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { _ in
                self.window.performClose(nil)
            }
        }
    }

    @objc func closeWarning() { window.performClose(nil) }

    func windowWillClose(_ notification: Notification) {
        guard !finished else { return }
        finished = true
        let value: [String: Any] = ["id": request.id, "acknowledged": true,
                                    "closed_at": ISO8601DateFormatter().string(from: Date())]
        do {
            let data = try JSONSerialization.data(withJSONObject: value)
            let path = URL(fileURLWithPath: request.ackPath)
            try data.write(to: path, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        } catch {
            fputs("Could not persist alert acknowledgment.\n", stderr)
        }
        NSApp.terminate(nil)
    }
}

let args = CommandLine.arguments
if let index = args.firstIndex(of: "--request"), args.count > index + 1 {
    do {
        let data = try Data(contentsOf: URL(fileURLWithPath: args[index + 1]))
        let request = try JSONDecoder().decode(Request.self, from: data)
        let app = NSApplication.shared
        let delegate = AlertDelegate(request: request, selfTest: args.contains("--self-test"))
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        app.run()
        withExtendedLifetime(delegate) {}
    } catch { fputs("Invalid alert request.\n", stderr); exit(1) }
} else { fputs("Expected --request path.\n", stderr); exit(2) }
