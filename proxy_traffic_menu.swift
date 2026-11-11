import AppKit
import SwiftUI
import Foundation

struct Target: Decodable {
    var host: String
    var upload_bps: Double
    var download_bps: Double
}
struct Snapshot: Decodable {
    var checked_at: String
    var sample_at: String?
    var status: String
    var blocked: Bool
    var last_error: String?
    var upload_bps: Double
    var download_bps: Double
    var proxy_bytes: Double
    var direct_bytes: Double
    var unknown_bytes: Double
    var historical_bytes: Double
    var budget_bytes: Double
    var next_warning_bytes: Double
    var started_at: String?
    var current_proxy_connections: Int?
    var top: [Target]
}
struct SpeedPoint { var up: Double; var down: Double }
struct AppUsage: Decodable {
    var process: String
    var upload: Double
    var download: Double
    var hosts: [String]?
    var processes: [String]?
}
struct UsageSummary: Decodable {
    var windows: Int
    var recorded_since: String?
    var recorded_until: String?
    var other_upload: Double
    var other_download: Double
    var top: [AppUsage]
}

func bytes(_ value: Double) -> String {
    let value = max(0, value)
    let units = ["KB", "MB", "GB", "TB"]
    var amount = value / 1000
    var unit = 0
    while amount >= 1000 && unit < units.count - 1 {
        amount /= 1000
        unit += 1
    }
    let format = unit >= 2 ? "%.2f" : amount >= 100 ? "%.0f" : "%.1f"
    return String(format: format, amount) + " " + units[unit]
}
func speed(_ value: Double) -> String { bytes(value) + "/s" }
func compactSpeed(_ value: Double) -> String {
    let units = ["KB", "MB", "GB", "TB"]
    var amount = max(0, value) / 1000
    var unit = 0
    while amount >= 1000 && unit < units.count - 1 {
        amount /= 1000
        unit += 1
    }
    let text = String(format: amount < 100 ? "%.1f" : "%.0f", amount) + units[unit]
    return String(repeating: " ", count: max(0, 6 - text.count)) + text
}

func isoDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let result = formatter.date(from: value) { return result }
    formatter.formatOptions = [.withInternetDateTime]
    return formatter.date(from: value)
}

final class MeterModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var history: [SpeedPoint] = []
    @Published var error: String?
    @Published var usage: UsageSummary?
    @Published var usagePeriod = 0
    @Published var usageLoading = false
    @Published var usageError: String?
    var refreshUsage: (() -> Void)?
    var dismissPanel: (() -> Void)?
    var fresh: Bool {
        guard let data = snapshot, data.last_error == nil, error == nil,
              let date = isoDate(data.sample_at) else { return false }
        return Date().timeIntervalSince(date) < 5
    }
    var statusText: String {
        if snapshot?.blocked == true { return "代理已切断" }
        return fresh ? "统计中" : snapshot == nil ? "等待采集" : "采集中断"
    }
    var statusColor: Color {
        if snapshot?.blocked == true { return .red }
        return fresh ? .green : .orange
    }
    var clock: String {
        formatClock(snapshot?.started_at)
    }
    func formatClock(_ value: String?) -> String {
        guard let date = isoDate(value) else { return "—" }
        let formatter = DateFormatter()
        formatter.dateFormat = "MM-dd HH:mm"
        formatter.timeZone = TimeZone(identifier: Bundle.main.object(forInfoDictionaryKey: "ProxyTrafficTimezone") as? String ?? "UTC")
        return formatter.string(from: date)
    }
}

struct Sparkline: View {
    let points: [SpeedPoint]
    var body: some View {
        GeometryReader { geometry in
            let maximum = max(1, points.map { max($0.up, $0.down) }.max() ?? 1)
            let width = geometry.size.width
            let height = geometry.size.height
            ZStack {
                Path { path in
                    path.move(to: CGPoint(x: 0, y: height - 1))
                    path.addLine(to: CGPoint(x: width, y: height - 1))
                }.stroke(Color.secondary.opacity(0.16), lineWidth: 1)
                ForEach(0..<2) { series in
                    Path { path in
                        for (index, value) in points.enumerated() {
                            let x = width * Double(index) / Double(max(1, points.count - 1))
                            let y = height - 3 - (height - 6) * (series == 0 ? value.down : value.up) / maximum
                            if index == 0 { path.move(to: CGPoint(x: x, y: y)) }
                            else { path.addLine(to: CGPoint(x: x, y: y)) }
                        }
                    }.stroke(series == 0 ? Color.teal : Color.orange,
                             style: StrokeStyle(lineWidth: 1.6, lineJoin: .round))
                }
            }
        }
    }
}

struct MeterView: View {
    @ObservedObject var model: MeterModel
    func row(_ title: String, _ value: Double, color: Color = .primary) -> some View {
        HStack {
            Text(title).foregroundColor(.secondary)
            Spacer()
            Text(bytes(value)).monospacedDigit().foregroundColor(color)
        }.font(.system(size: 12))
    }
    var usageSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("软件累计排行").font(.system(size: 13, weight: .semibold))
                Spacer()
                if model.usageLoading {
                    ProgressView().controlSize(.mini)
                }
            }
            Picker("累计范围", selection: $model.usagePeriod) {
                Text("今日").tag(0)
                Text("近 72 小时").tag(1)
            }.pickerStyle(.segmented)
                .onChange(of: model.usagePeriod) { _ in
                    model.usage = nil
                    model.refreshUsage?()
                }
            if let usage = model.usage, !usage.top.isEmpty {
                Text("上传 + 下载 · 自 " + model.formatClock(usage.recorded_since) + " 起记录")
                    .font(.system(size: 10)).foregroundColor(.secondary)
                ForEach(Array(usage.top.enumerated()), id: \.offset) { index, app in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 8) {
                            Text("\(index + 1)").monospacedDigit()
                                .foregroundColor(.secondary).frame(width: 16, alignment: .trailing)
                            Text(app.process).fontWeight(.medium).lineLimit(1)
                            Spacer(minLength: 4)
                            Text(bytes(app.upload + app.download)).monospacedDigit().fontWeight(.medium)
                        }.font(.system(size: 12))
                        HStack(spacing: 12) {
                            Text("↓ " + bytes(app.download)).foregroundColor(.teal)
                            Text("↑ " + bytes(app.upload)).foregroundColor(.orange)
                        }.font(.system(size: 10)).monospacedDigit().padding(.leading, 24)
                    }
                    .padding(.vertical, 3)
                    .help((app.processes ?? []).joined(separator: "、") + "\n" +
                          (app.hosts ?? []).joined(separator: "\n"))
                }
                Text("按已保存的流量记录累计；较早用量及未识别来源无法补算到软件。")
                    .font(.system(size: 10)).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if usage.other_upload + usage.other_download > 0 {
                    Text("其余目标合计 " + bytes(usage.other_upload + usage.other_download))
                        .font(.system(size: 10)).foregroundColor(.secondary)
                }
            } else if let error = model.usageError {
                Text(error).font(.system(size: 11)).foregroundColor(.secondary)
            } else {
                Text(model.usageLoading ? "正在读取累计记录…" : "等待新的流量记录")
                    .font(.system(size: 11)).foregroundColor(.secondary)
            }
        }
    }
    var body: some View {
        ScrollView(.vertical, showsIndicators: true) {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Image(systemName: "arrow.up.arrow.down.circle.fill")
                    .font(.system(size: 23)).foregroundColor(.teal)
                Text("代理流量").font(.system(size: 18, weight: .semibold))
                Spacer()
                Circle().fill(model.statusColor).frame(width: 6, height: 6)
                Text(model.statusText).font(.system(size: 11)).foregroundColor(.secondary)
                Button { model.dismissPanel?() } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold))
                }.buttonStyle(.plain).foregroundColor(.secondary).help("关闭面板（Esc）")
                    .accessibilityLabel("关闭代理流量面板")
            }
            HStack(spacing: 25) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("↓ 下载").font(.system(size: 11)).foregroundColor(.secondary)
                    Text(model.fresh ? speed(model.snapshot?.download_bps ?? 0) : "—")
                        .font(.system(size: 20, weight: .medium, design: .rounded))
                        .monospacedDigit().foregroundColor(.teal)
                }
                Spacer(minLength: 0)
                VStack(alignment: .leading, spacing: 4) {
                    Text("↑ 上传").font(.system(size: 11)).foregroundColor(.secondary)
                    Text(model.fresh ? speed(model.snapshot?.upload_bps ?? 0) : "—")
                        .font(.system(size: 20, weight: .medium, design: .rounded))
                        .monospacedDigit().foregroundColor(.orange)
                }
            }
            Sparkline(points: model.history).frame(height: 43)
            VStack(spacing: 9) {
                HStack {
                    Text("今日已确认代理").font(.system(size: 13, weight: .medium))
                    Spacer()
                    Text(bytes(model.snapshot?.proxy_bytes ?? 0))
                        .font(.system(size: 19, weight: .semibold, design: .rounded)).monospacedDigit()
                }
                row("已排除直连", model.snapshot?.direct_bytes ?? 0)
                row("未归类余量", model.snapshot?.unknown_bytes ?? 0, color: .orange)
                    .help("短连接及采集间隔内无法确认分类的余量。单独列出，并计入告警预算，避免漏报。")
                if let old = model.snapshot?.historical_bytes, old > 0 {
                    row("启用前未分类", old)
                        .help("升级前只有 Clash 总计，无法还原代理与直连。保留到今天结束，避免重置告警。")
                }
            }
            Divider()
            usageSection
            Divider()
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text("告警预算").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Text(bytes(model.snapshot?.budget_bytes ?? 0) + " / " +
                         bytes(model.snapshot?.next_warning_bytes ?? 10_000_000_000))
                        .font(.system(size: 11)).monospacedDigit().foregroundColor(.secondary)
                }
                ProgressView(value: min(1, (model.snapshot?.budget_bytes ?? 0) /
                                        max(1, model.snapshot?.next_warning_bytes ?? 10_000_000_000)))
                    .tint(model.snapshot?.blocked == true ? .red : .teal)
                Text("包含未归类及启用前用量；已确认直连不计入。")
                    .font(.system(size: 10)).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    Text("当前代理目标").font(.system(size: 12, weight: .medium))
                    Spacer()
                    Text("\(model.snapshot?.current_proxy_connections ?? 0) 个连接")
                        .font(.system(size: 10)).foregroundColor(.secondary)
                }
                if let top = model.snapshot?.top, !top.isEmpty, model.fresh {
                    ForEach(Array(top.prefix(3).enumerated()), id: \.offset) { _, target in
                        HStack {
                            Text(target.host).lineLimit(1).truncationMode(.middle)
                            Spacer(minLength: 8)
                            Text(speed(target.download_bps + target.upload_bps)).monospacedDigit()
                                .foregroundColor(.secondary)
                        }.font(.system(size: 11))
                    }
                } else {
                    Text(model.fresh ? "暂时没有活跃的代理传输" : "等待采集服务恢复")
                        .font(.system(size: 11)).foregroundColor(.secondary)
                }
            }.frame(minHeight: 64, alignment: .top)
            Divider()
            HStack {
                Text("统计起点  " + model.clock)
                Spacer()
                Text("本机估算").help("按已识别代理连接统计，不包含代理协议开销或机场计费倍率。")
            }.font(.system(size: 10)).foregroundColor(.secondary)
        }
        .padding(18)
        }
        .frame(width: 344, height: 600)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    let model = MeterModel()
    var item: NSStatusItem!
    let popover = NSPopover()
    var timer: Timer?
    var lastStamp = ""
    var globalClickMonitor: Any?
    var localEventMonitor: Any?
    var lastUsagePoll = Date.distantPast
    var usageRequest = UUID()
    let args = CommandLine.arguments
    var runtimePath: String {
        Bundle.main.object(forInfoDictionaryKey: "ProxyTrafficRuntime") as? String ??
            NSHomeDirectory() + "/Library/Application Support/ProxyTrafficMeter"
    }
    var statusPath: String {
        if let index = args.firstIndex(of: "--status"), args.count > index + 1 { return args[index+1] }
        return runtimePath + "/data/meter-status.json"
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: 84)
        if let button = item.button {
            button.title = "代理 · 等待采集"
            button.target = self
            button.action = #selector(toggle)
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
            button.toolTip = "代理上下行速度 · 点击查看今日用量"
            button.setAccessibilityLabel("代理流量监控")
        }
        popover.contentViewController = NSHostingController(rootView: MeterView(model: model))
        popover.behavior = .transient
        popover.animates = false
        popover.delegate = self
        popover.contentSize = NSSize(width: 344, height: 600)
        model.dismissPanel = { [weak self] in self?.closePanel() }
        model.refreshUsage = { [weak self] in self?.pollUsage(force: true) }
        if let index = args.firstIndex(of: "--render-preview"), args.count > index + 1 {
            renderPreview(args[index+1])
            return
        }
        writePID()
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in self.poll() }
    }
    func writePID() {
        guard !args.contains("--render-preview") else { return }
        let path = runtimePath + "/data/menu.pid.json"
        if let data = try? JSONSerialization.data(withJSONObject: ["pid": ProcessInfo.processInfo.processIdentifier,
                                                                  "bundle": "dev.proxytraffic.meter",
                                                                  "panel_open": popover.isShown,
                                                                  "outside_click_monitor": globalClickMonitor != nil,
                                                                  "ranked_apps": model.usage?.top.count ?? 0,
                                                                  "ranking_period": model.usagePeriod]) {
            try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
        }
    }
    func poll() {
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: statusPath))
            let snapshot = try JSONDecoder().decode(Snapshot.self, from: data)
            model.snapshot = snapshot
            model.error = nil
            if lastStamp != snapshot.checked_at {
                lastStamp = snapshot.checked_at
                model.history.append(SpeedPoint(up: model.fresh ? snapshot.upload_bps : 0,
                                                down: model.fresh ? snapshot.download_bps : 0))
                if model.history.count > 60 { model.history.removeFirst(model.history.count - 60) }
            }
        } catch {
            model.error = "采集服务尚未就绪"
        }
        updateTitle()
        if popover.isShown { pollUsage() }
    }
    func pollUsage(force: Bool = false) {
        guard force || Date().timeIntervalSince(lastUsagePoll) >= 10 else { return }
        lastUsagePoll = Date()
        let request = UUID()
        usageRequest = request
        let period = model.usagePeriod
        model.usageLoading = true
        model.usageError = nil
        let script = runtimePath + "/traffic_history.py"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let process = Process()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: Bundle.main.object(forInfoDictionaryKey: "ProxyTrafficPython") as? String ?? "/usr/bin/python3")
            process.arguments = [script, "--json", "--group", "software", "--sort", "total", "--top", "100"] +
                (period == 0 ? ["--today"] : ["--hours", "72"])
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            var summary: UsageSummary?
            do {
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus == 0 {
                    summary = try JSONDecoder().decode(UsageSummary.self, from: data)
                }
            } catch {}
            let loadedSummary = summary
            DispatchQueue.main.async { [weak self] in
                guard let self, self.usageRequest == request, self.model.usagePeriod == period else { return }
                self.model.usageLoading = false
                self.model.usage = loadedSummary
                self.model.usageError = loadedSummary == nil ? "累计记录暂不可读，稍后自动重试" : nil
                self.writePID()
            }
        }
    }
    func updateTitle() {
        guard let button = item.button else { return }
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        style.lineSpacing = 0
        style.minimumLineHeight = 10
        style.maximumLineHeight = 10
        let title: String
        if let value = model.snapshot, value.blocked {
            title = "代理已切断\n今日 \(bytes(value.proxy_bytes))"
        } else if let value = model.snapshot, model.fresh {
            title = "↓\(compactSpeed(value.download_bps))↑\(compactSpeed(value.upload_bps))\n代理 \(bytes(value.proxy_bytes))"
        } else {
            title = "代理 · \(model.statusText)\n点击查看"
        }
        button.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 8.5, weight: .medium),
            .paragraphStyle: style,
            .foregroundColor: NSColor.labelColor
        ])
    }
    @objc func toggle() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            let open = NSMenuItem(title: "打开代理流量面板", action: #selector(show), keyEquivalent: "")
            open.target = self
            menu.addItem(open)
            menu.addItem(.separator())
            let quit = NSMenuItem(title: "退出显示（后台告警继续运行）", action: #selector(quit), keyEquivalent: "")
            quit.target = self
            menu.addItem(quit)
            if let event = NSApp.currentEvent, let button = item.button {
                NSMenu.popUpContextMenu(menu, with: event, for: button)
            }
            return
        }
        if popover.isShown { closePanel() } else { show() }
    }
    @objc func show() {
        guard let button = item.button else { return }
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        installDismissMonitors()
        pollUsage(force: true)
        writePID()
    }
    func installDismissMonitors() {
        removeDismissMonitors()
        globalClickMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
                self?.closePanel()
            }
        localEventMonitor = NSEvent.addLocalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown, .keyDown]) { [weak self] event in
                guard let self, self.popover.isShown else { return event }
                if event.type == .keyDown {
                    if event.keyCode == 53 { self.closePanel(); return nil }
                    return event
                }
                if event.window === self.popover.contentViewController?.view.window { return event }
                if let button = self.item.button, event.window === button.window,
                   button.bounds.contains(button.convert(event.locationInWindow, from: nil)) { return event }
                self.closePanel()
                return event
            }
    }
    func removeDismissMonitors() {
        if let monitor = globalClickMonitor { NSEvent.removeMonitor(monitor) }
        if let monitor = localEventMonitor { NSEvent.removeMonitor(monitor) }
        globalClickMonitor = nil
        localEventMonitor = nil
    }
    func closePanel() { popover.close() }
    func popoverDidClose(_ notification: Notification) {
        removeDismissMonitors()
        writePID()
    }
    func applicationDidResignActive(_ notification: Notification) {
        if popover.isShown { closePanel() }
    }
    func applicationWillTerminate(_ notification: Notification) { removeDismissMonitors() }
    @objc func quit() { NSApp.terminate(nil) }

    func verifyInteractions(_ window: NSWindow, path: String, completion: @escaping () -> Void) {
        var result: [String: Bool] = [:]
        func findScroll(_ view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView { return scroll }
            for child in view.subviews { if let found = findScroll(child) { return found } }
            return nil
        }
        if let view = window.contentView, let scroll = findScroll(view),
           let document = scroll.documentView {
            let old = scroll.contentView.bounds.origin.y
            let next = old > 0 ? 0 : max(0, document.bounds.height-scroll.contentView.bounds.height)
            scroll.contentView.scroll(to: NSPoint(x: 0, y: next))
            scroll.reflectScrolledClipView(scroll.contentView)
            result["scroll_moves_content"] = abs(scroll.contentView.bounds.origin.y-old) > 1
        }
        show()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            result["panel_opens"] = self.popover.isShown
            result["global_and_local_monitors_installed"] =
                self.globalClickMonitor != nil && self.localEventMonitor != nil
            // Post only to this application's own queue and own preview window.
            for type: NSEvent.EventType in [.leftMouseDown, .leftMouseUp] {
                if let event = NSEvent.mouseEvent(
                    with: type, location: NSPoint(x: 12, y: 12), modifierFlags: [],
                    timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                    context: nil, eventNumber: 0, clickCount: 1, pressure: 0) {
                    NSApp.postEvent(event, atStart: false)
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                result["outside_click_closes"] = !self.popover.isShown
                result["monitors_removed_after_close"] =
                    self.globalClickMonitor == nil && self.localEventMonitor == nil
                self.show()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                    if let event = NSEvent.keyEvent(
                        with: .keyDown, location: .zero, modifierFlags: [],
                        timestamp: ProcessInfo.processInfo.systemUptime,
                        windowNumber: self.popover.contentViewController?.view.window?.windowNumber ?? 0,
                        context: nil, characters: "\u{1b}", charactersIgnoringModifiers: "\u{1b}",
                        isARepeat: false, keyCode: 53) {
                        NSApp.postEvent(event, atStart: false)
                    }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                        result["escape_closes"] = !self.popover.isShown
                        self.show()
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                            self.applicationDidResignActive(Notification(name: NSApplication.didResignActiveNotification))
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                                result["resign_active_closes"] = !self.popover.isShown
                                if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
                                    let url = URL(fileURLWithPath: path).deletingPathExtension()
                                        .appendingPathExtension("interactions.json")
                                    try? data.write(to: url, options: .atomic)
                                    print("NATIVE_INTERACTIONS " + String(data: data, encoding: .utf8)!)
                                }
                                completion()
                            }
                        }
                    }
                }
            }
        }
    }

    func renderPreview(_ path: String) {
        let now = ISO8601DateFormatter().string(from: Date())
        let fixture: [String: Any] = [
            "checked_at": now, "sample_at": now, "status": "monitoring", "blocked": false,
            "upload_bps": 12340, "download_bps": 845600, "proxy_bytes": 1456123456,
            "direct_bytes": 2378123456, "unknown_bytes": 12456, "historical_bytes": 4650000000,
            "budget_bytes": 6106135912, "next_warning_bytes": 10000000000,
            "started_at": now, "current_proxy_connections": 7,
            "top": [["host": "chatgpt.com", "upload_bps": 12340, "download_bps": 800000],
                    ["host": "api.anthropic.com", "upload_bps": 0, "download_bps": 45600]]
        ]
        model.snapshot = try? JSONDecoder().decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: fixture))
        model.history = (0..<60).map { index in
            SpeedPoint(up: 10000 + 6000 * sin(Double(index) / 3),
                       down: 200000 + 600000 * abs(sin(Double(index) / 8)))
        }
        let names = ["Codex / ChatGPT", "Google Chrome", "Antigravity / agy",
                     "Microsoft Edge", "本地代理网关（转发）", "未识别进程"]
        let sampleApps: [AppUsage] = (0..<18).map { index in
            let divisor = Double(index + 1)
            let suffix = index < 6 ? "" : " \(index + 1)"
            return AppUsage(process: names[index % names.count] + suffix,
                            upload: 210_000_000.0 / divisor, download: 860_000_000.0 / divisor,
                            hosts: ["example.test"], processes: ["example"])
        }
        model.usage = UsageSummary(windows: 100, recorded_since: now, recorded_until: now,
                                  other_upload: 3200, other_download: 6500, top: sampleApps)
        updateTitle()
        let controller = NSHostingController(rootView: MeterView(model: model))
        let view = controller.view
        view.setFrameSize(view.fittingSize)
        let window = NSWindow(contentRect: view.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.contentViewController = controller
        window.center()
        window.orderFront(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) {
            if self.args.contains("--preview-bottom") {
                func scrollView(_ node: NSView) -> NSScrollView? {
                    if let scroll = node as? NSScrollView { return scroll }
                    for child in node.subviews { if let result = scrollView(child) { return result } }
                    return nil
                }
                if let scroll = scrollView(view), let document = scroll.documentView {
                    scroll.contentView.scroll(to: NSPoint(
                        x: 0, y: max(0, document.bounds.height - scroll.contentView.bounds.height)))
                    scroll.reflectScrolledClipView(scroll.contentView)
                }
            }
            view.layoutSubtreeIfNeeded()
            view.displayIfNeeded()
            if let image = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: image)
                if let data = image.representation(using: .png, properties: [:]) {
                    do {
                        try data.write(to: URL(fileURLWithPath: path), options: .atomic)
                        print("PREVIEW_OK \(Int(view.bounds.width))x\(Int(view.bounds.height))")
                    } catch { fputs("Preview write failed.\n", stderr) }
                }
            }
            if let button = self.item.button,
               let image = button.bitmapImageRepForCachingDisplay(in: button.bounds) {
                button.cacheDisplay(in: button.bounds, to: image)
                let destination = URL(fileURLWithPath: path).deletingPathExtension().appendingPathExtension("bar.png")
                if let data = image.representation(using: .png, properties: [:]) {
                    try? data.write(to: destination, options: .atomic)
                }
                print("MENU_APPEARANCE width=\(self.item.length) font=8.5")
            }
            let finish = { window.orderOut(nil); NSApp.terminate(nil) }
            if self.args.contains("--self-test-interactions") {
                self.verifyInteractions(window, path: path, completion: finish)
            } else {
                finish()
            }
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.setActivationPolicy(.accessory)
app.delegate = delegate
app.run()
withExtendedLifetime(delegate) {}
