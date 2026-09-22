// The menu bar app: `rfa up`, `rfa restart` and `rfa down` without a terminal.
//
// It owns nothing. Every button is the command you would type, run against the checkout in
// `RFAHome` (written into Info.plist by ui/build.sh), and everything it draws comes from
// `rfa status --json`. So the menu and the terminal can never disagree, and quitting the app
// leaves the daemon running -- it is a window onto the pipeline, not the pipeline.
//
// Build: ui/build.sh

import AppKit
import Carbon.HIToolbox
import ServiceManagement
import UserNotifications
import WebKit

// The capture hotkey. Change it here.
let hotKeyCode = UInt32(kVK_Space)
let hotKeyModifiers = UInt32(optionKey)

let home: URL = {
    guard let path = Bundle.main.object(forInfoDictionaryKey: "RFAHome") as? String else {
        fatalError("RFAHome missing from Info.plist; build with ui/build.sh")
    }
    return URL(fileURLWithPath: path)
}()

let stageOrder = ["draft", "planning", "todo", "under-work", "done"]

// MARK: - What `rfa status --json` says

struct Status: Decodable {
    struct Gate: Decodable { let name: String; let ok: Bool; let detail: String }
    struct Card: Decodable { let id: String; let title: String; let status: String }
    struct Job: Decodable { let job: String; let id: String; let title: String }

    let home: String
    let model: String
    let models: [String]
    let reasoning: [String]
    let defaultReasoning: String
    let repos: [String]
    let boardUrl: String
    let services: [String: Int]  // 0 when that one is not running
    let gates: [Gate]
    let stages: [String: Int]
    let running: [Card]
    let next: Job?
    let last: Card?

    var daemonUp: Bool { (services["daemon"] ?? 0) > 0 }
    var boardUp: Bool { (services["board"] ?? 0) > 0 }
    var blocked: [Gate] { gates.filter { !$0.ok } }
}

// MARK: - Running rfa

enum Rfa {
    static let script = home.appendingPathComponent("bin/rfa").path

    /// Blocks: call it off the main thread. `rfa up` can sit for minutes pulling a Docker image.
    static func run(_ arguments: [String]) -> (ok: Bool, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: script)
        process.arguments = arguments
        process.currentDirectoryURL = home
        var environment = ProcessInfo.processInfo.environment
        // An app launched from Finder inherits no shell PATH, and `rfa up` needs docker, ollama and git.
        let search = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        environment["PATH"] = (search + [environment["PATH"] ?? ""]).joined(separator: ":")
        environment["RFA_HOME"] = home.path
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do { try process.run() } catch { return (false, error.localizedDescription) }
        // Drained before waiting: a command that outruns the pipe buffer would otherwise deadlock.
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus == 0, String(data: data, encoding: .utf8) ?? "")
    }

    /// A command that prints JSON. Anything else is a failure, whatever its exit code.
    static func json(_ arguments: [String]) -> [String: Any]? {
        let result = run(arguments)
        guard result.ok, let data = result.output.data(using: .utf8) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    /// The page can only ask about things that look like what rfa.yaml holds. `rfa` checks them
    /// again and refuses what it does not know -- this just keeps the obvious nonsense out of argv.
    static func isRepoId(_ text: String) -> Bool {
        text.range(of: "^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", options: .regularExpression) != nil
    }

    static func isRepoBranch(_ text: String) -> Bool {
        let parts = text.split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, isRepoId(String(parts[0])) else { return false }
        let branch = String(parts[1])
        return branch.range(of: "^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$", options: .regularExpression) != nil
            && !branch.contains("..") && !branch.hasSuffix(".lock")
    }

    static func status() -> Status? {
        let result = run(["status", "--json"])
        guard let data = result.output.data(using: .utf8) else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(Status.self, from: data)
    }
}

// MARK: - The capture overlay

final class OverlayPanel: NSPanel {
    var onCancel: (() -> Void)?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }

    /// Esc in a borderless panel reaches the end of the responder chain unhandled, and an unhandled
    /// key is a system beep. WebKit lets it out as either the command or the raw key, so both are
    /// answered here; the page closes the box too, and closing twice is closing once.
    override func cancelOperation(_ sender: Any?) { onCancel?() }

    override func keyDown(with event: NSEvent) {
        if event.keyCode == UInt16(kVK_Escape) { onCancel?() } else { super.keyDown(with: event) }
    }
}

/// ⌥Space anywhere: a box to type an idea into, with the repositories and their branches.
///
/// It holds no rules of its own. Every question it asks and every draft it writes goes through
/// `rfa`, so what the overlay captures and what `rfa new` writes cannot drift apart.
final class Overlay: NSObject, WKScriptMessageHandlerWithReply {
    let panel: OverlayPanel
    let webView: WKWebView

    override init() {
        panel = OverlayPanel(
            contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .modalPanel
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.hidesOnDeactivate = false

        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.setValue(false, forKey: "drawsBackground")
        super.init()
        config.userContentController.addScriptMessageHandler(self, contentWorld: .page, name: "rfa")

        let blur = NSVisualEffectView()
        blur.material = .hudWindow
        blur.blendingMode = .behindWindow
        blur.state = .active
        blur.alphaValue = 0.55

        let container = NSView()
        container.addSubview(blur)
        container.addSubview(webView)
        for view in [blur, webView] as [NSView] {
            view.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([
                view.leadingAnchor.constraint(equalTo: container.leadingAnchor),
                view.trailingAnchor.constraint(equalTo: container.trailingAnchor),
                view.topAnchor.constraint(equalTo: container.topAnchor),
                view.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            ])
        }
        panel.contentView = container

        panel.onCancel = { [weak self] in self?.hide() }

        let ui = home.appendingPathComponent("ui")
        webView.loadFileURL(ui.appendingPathComponent("capture.html"), allowingReadAccessTo: ui)
    }

    var isVisible: Bool { panel.isVisible }

    func show() {
        let screen = NSScreen.screens.first { NSMouseInRect(NSEvent.mouseLocation, $0.frame, false) } ?? NSScreen.main!
        panel.setFrame(screen.frame, display: true)
        panel.alphaValue = 0
        NSApp.activate(ignoringOtherApps: true)
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(webView)
        NSAnimationContext.runAnimationGroup { $0.duration = 0.15; panel.animator().alphaValue = 1 }
        webView.evaluateJavaScript("window.rfa && window.rfa.onShow()")
    }

    func hide() {
        guard panel.isVisible else { return }
        NSAnimationContext.runAnimationGroup({ $0.duration = 0.12; panel.animator().alphaValue = 0 }) {
            self.panel.orderOut(nil)
            NSApp.hide(nil)
        }
    }

    func toggle() { isVisible ? hide() : show() }

    /// The page asks; `rfa` answers. Everything here runs off the main thread, because `rfa
    /// branches` goes to each repository's remote and a spinning capture box is a useless one.
    func userContentController(
        _ controller: WKUserContentController, didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else {
            return replyHandler(nil, "bad message")
        }
        switch type {
        case "close":
            hide()
            replyHandler(nil, nil)
        case "setup":
            // One call: the box needs the repositories, the models and the levels before it can draw.
            background({ Rfa.status() }) { status in
                replyHandler([
                    "repos": status?.repos ?? [],
                    "models": status?.models ?? [],
                    "model": status?.model ?? "",
                    "reasoning": status?.reasoning ?? [],
                    "defaultReasoning": status?.defaultReasoning ?? "",
                ], nil)
            }
        case "branches":
            let repos = (body["repos"] as? [String] ?? []).filter(Rfa.isRepoId)
            guard !repos.isEmpty else { return replyHandler(["branches": [], "errors": []], nil) }
            background({ Rfa.json(["branches"] + repos + ["--json"]) }) { found in
                replyHandler(found ?? ["branches": [], "errors": ["rfa branches failed"]], nil)
            }
        case "save":
            let idea = (body["idea"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let repos = (body["repos"] as? [String] ?? []).filter(Rfa.isRepoId)
            guard !idea.isEmpty else { return replyHandler(["error": "say what should happen"], nil) }
            guard !repos.isEmpty else { return replyHandler(["error": "pick a repository"], nil) }
            var args = ["new", idea]
            for repo in repos { args += ["-r", repo] }
            for pick in (body["branches"] as? [String] ?? []).filter(Rfa.isRepoBranch) { args += ["-b", pick] }
            for pick in (body["context_branches"] as? [String] ?? []).filter(Rfa.isRepoBranch) { args += ["-c", pick] }
            // Blank means the workspace default; `rfa new` refuses anything it does not know.
            if let model = body["model"] as? String, !model.isEmpty { args += ["-m", model] }
            if let level = body["reasoning"] as? String, !level.isEmpty { args += ["-R", level] }
            background({ Rfa.run(args) }) { result in
                replyHandler(result.ok ? ["ok": true] : ["error": String(result.output.suffix(300))], nil)
            }
        default:
            replyHandler(nil, "unknown message \(type)")
        }
    }

    private func background<T>(_ work: @escaping () -> T, _ done: @escaping (T) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let value = work()
            DispatchQueue.main.async { done(value) }
        }
    }
}

// MARK: - Edit menu

/// ⌘Z/⌘X/⌘C/⌘V/⌘A reach a WKWebView only as key equivalents of menu items. This app is an
/// accessory, so the menu bar never shows it; the menu exists for its shortcuts alone.
enum EditMenu {
    static func install() {
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let item = NSMenuItem()
        item.submenu = edit
        let menu = NSMenu()
        menu.addItem(item)
        NSApp.mainMenu = menu
    }
}

// MARK: - The menu

final class Controller: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let menu = NSMenu()
    var status: Status?
    var overlay: Overlay?
    var busy: String?
    var hotKeyProblem: String?
    var timer: Timer?
    var isOpen = false
    var announced: String?

    func start() {
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        draw()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) {
        isOpen = true
        refresh()
    }

    func menuDidClose(_ menu: NSMenu) { isOpen = false }

    func refresh() {
        DispatchQueue.global(qos: .utility).async {
            let status = Rfa.status()
            DispatchQueue.main.async {
                self.announce(status?.last)
                self.status = status
                self.draw()
            }
        }
    }

    /// The first poll after launch only learns which run was last; it is not news.
    func announce(_ last: Status.Card?) {
        guard let last, announced != last.id else { return }
        let first = announced == nil && status == nil
        announced = last.id
        guard !first else { return }
        Notify.post(last.status == "shipped" ? "Shipped" : "Run failed", last.title)
    }

    // MARK: drawing

    var symbol: String {
        if busy != nil { return "arrow.triangle.2.circlepath" }
        guard let status, status.daemonUp else { return "circle.dashed" }
        if !status.running.isEmpty { return "hammer.fill" }
        if !status.blocked.isEmpty { return "pause.circle" }
        return "checkmark.circle"
    }

    var headline: String {
        if let busy { return "\(busy)…" }
        guard let status else { return "rfa — no answer from \(Rfa.script)" }
        if !status.daemonUp { return "Stopped" }
        if let card = status.running.first { return "Running — \(card.title)" }
        if let blocked = status.blocked.first { return "Holding — \(blocked.detail)" }
        if let next = status.next { return "Next — \(next.job) \(next.title)" }
        return "Idle — nothing waiting"
    }

    func draw() {
        item.button?.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "rfa")
        // Never rebuild under the pointer: the click already on its way would land on whichever
        // item the rebuild happened to put there, and one of them is Down.
        guard !isOpen || menu.highlightedItem == nil else { return }
        menu.removeAllItems()
        menu.addItem(note(headline, bold: true))
        if let status {
            for gate in status.gates {
                menu.addItem(note("\(gate.ok ? "✓" : "✗")  \(gate.name) — \(gate.detail)"))
            }
            menu.addItem(note(stageOrder.map { "\($0) \(status.stages[$0] ?? 0)" }.joined(separator: "   ")))
        }
        menu.addItem(.separator())
        let up = status?.daemonUp ?? false
        menu.addItem(action("Up", #selector(bringUp), enabled: !up))
        menu.addItem(action("Restart", #selector(restart), enabled: up))
        menu.addItem(action("Down", #selector(bringDown), enabled: up))
        menu.addItem(.separator())
        menu.addItem(action(hotKeyProblem == nil ? "New Idea    ⌥Space" : "New Idea", #selector(capture)))
        if let hotKeyProblem { menu.addItem(note("   \(hotKeyProblem) — use this item instead")) }
        menu.addItem(models())
        menu.addItem(reasoning())
        menu.addItem(.separator())
        menu.addItem(action("Open Board", #selector(openBoard), enabled: status?.boardUp ?? false))
        menu.addItem(action("Open Tasks Folder", #selector(openTasks)))
        menu.addItem(action("Open Daemon Log", #selector(openLog)))
        menu.addItem(.separator())
        let login = action("Open at Login", #selector(toggleLogin))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        // No target: `terminate:` goes up the responder chain to NSApp. Always enabled -- quitting
        // the menu leaves the daemon running, which is the point of it being a separate process.
        let quit = NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(quit)
    }

    /// Picking one writes it down for the next run; it never interrupts one already going.
    func models() -> NSMenuItem {
        let item = NSMenuItem(title: "Model", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        for name in status?.models ?? [] {
            let choice = NSMenuItem(title: name, action: #selector(chooseModel(_:)), keyEquivalent: "")
            choice.target = self
            choice.isEnabled = busy == nil
            choice.state = name == status?.model ? .on : .off
            submenu.addItem(choice)
        }
        if submenu.items.isEmpty { submenu.addItem(note("none in rfa.yaml")) }
        item.submenu = submenu
        item.isEnabled = true
        return item
    }

    /// The same, for the reasoning level: `rfa reasoning` is the command behind it.
    func reasoning() -> NSMenuItem {
        let item = NSMenuItem(title: "Reasoning", action: nil, keyEquivalent: "")
        let submenu = NSMenu()
        submenu.autoenablesItems = false
        for level in status?.reasoning ?? [] {
            let choice = NSMenuItem(title: level, action: #selector(chooseReasoning(_:)), keyEquivalent: "")
            choice.target = self
            choice.isEnabled = busy == nil
            choice.state = level == status?.defaultReasoning ? .on : .off
            submenu.addItem(choice)
        }
        if submenu.items.isEmpty { submenu.addItem(note("none in rfa.yaml")) }
        item.submenu = submenu
        item.isEnabled = true
        return item
    }

    func note(_ text: String, bold: Bool = false) -> NSMenuItem {
        let item = NSMenuItem(title: text, action: nil, keyEquivalent: "")
        let font = bold ? NSFont.menuBarFont(ofSize: 0) : NSFont.menuFont(ofSize: NSFont.smallSystemFontSize)
        item.attributedTitle = NSAttributedString(string: text, attributes: [.font: font])
        item.isEnabled = false
        return item
    }

    func action(_ title: String, _ selector: Selector, enabled: Bool = true) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: selector, keyEquivalent: "")
        item.target = self
        item.isEnabled = enabled && busy == nil
        return item
    }

    // MARK: commands

    /// One at a time, because they are the same three commands and they contradict each other.
    func command(_ name: String, _ arguments: [String]) {
        guard busy == nil else { return }
        busy = name
        draw()
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Rfa.run(arguments)
            DispatchQueue.main.async {
                self.busy = nil
                if !result.ok { self.complain(name, result.output) }
                self.refresh()
            }
        }
    }

    // `up` checks Docker, Ollama and the repositories, and pulls what is missing, so it is the slow
    // one; `--no-open` because the board is its own menu item.
    @objc func bringUp() { command("Bringing rfa up", ["up", "--no-open"]) }
    @objc func restart() { command("Restarting", ["restart"]) }
    @objc func bringDown() { command("Stopping", ["down"]) }

    @objc func capture() { overlay?.show() }

    @objc func chooseModel(_ sender: NSMenuItem) { command("Switching to \(sender.title)", ["model", sender.title]) }
    @objc func chooseReasoning(_ sender: NSMenuItem) { command("Switching to \(sender.title)", ["reasoning", sender.title]) }

    @objc func openBoard() { NSWorkspace.shared.open(URL(string: status?.boardUrl ?? "http://127.0.0.1:4380/")!) }
    @objc func openTasks() { NSWorkspace.shared.open(home.appendingPathComponent("tasks")) }
    @objc func openLog() { NSWorkspace.shared.open(home.appendingPathComponent("var/daemon.log")) }

    @objc func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            complain("Open at Login", error.localizedDescription)
        }
        draw()
    }

    func complain(_ title: String, _ detail: String) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "\(title) failed"
        alert.informativeText = detail.isEmpty ? "No output." : String(detail.suffix(1500))
        alert.runModal()
    }
}

// MARK: - Notifications

/// A run takes long enough that you go and do something else, which is exactly when you stop
/// looking at the menu bar. `report()["last"]` names the run that finished most recently, so a
/// change in it is a run that ended since the last look.
enum Notify {
    static func ask() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    static func post(_ title: String, _ body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    let controller = Controller()
    var overlay: Overlay!
    var hotKey: EventHotKeyRef?

    func applicationDidFinishLaunching(_ note: Notification) {
        EditMenu.install()
        overlay = Overlay()
        controller.overlay = overlay
        controller.start()
        Notify.ask()
        registerHotKey()
    }

    /// Registration fails when another app already owns the combination, and it fails quietly --
    /// so the status is kept and the menu says so, rather than leaving you pressing a dead key.
    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, context in
            let delegate = Unmanaged<AppDelegate>.fromOpaque(context!).takeUnretainedValue()
            DispatchQueue.main.async { delegate.overlay.toggle() }
            return noErr
        }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
        let id = EventHotKeyID(signature: OSType(0x5246_4141), id: 1)  // 'RFAA'
        let status = RegisterEventHotKey(hotKeyCode, hotKeyModifiers, id, GetApplicationEventTarget(), 0, &hotKey)
        controller.hotKeyProblem = status == noErr ? nil : "⌥Space is taken (OSStatus \(status))"
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
