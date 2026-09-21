// The menu bar app: `rfa up`, `rfa restart` and `rfa down` without a terminal.
//
// It owns nothing. Every button is the command you would type, run against the checkout in
// `RFAHome` (written into Info.plist by ui/build.sh), and everything it draws comes from
// `rfa status --json`. So the menu and the terminal can never disagree, and quitting the app
// leaves the daemon running -- it is a window onto the pipeline, not the pipeline.
//
// Build: ui/build.sh

import AppKit
import ServiceManagement

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
    let boardUrl: String
    let services: [String: Int]  // 0 when that one is not running
    let gates: [Gate]
    let stages: [String: Int]
    let running: [Card]
    let next: Job?

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

    static func status() -> Status? {
        let result = run(["status", "--json"])
        guard let data = result.output.data(using: .utf8) else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try? decoder.decode(Status.self, from: data)
    }
}

// MARK: - The menu

final class Controller: NSObject, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    let menu = NSMenu()
    var status: Status?
    var busy: String?
    var timer: Timer?
    var isOpen = false

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
                self.status = status
                self.draw()
            }
        }
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
        menu.addItem(models())
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

    @objc func chooseModel(_ sender: NSMenuItem) { command("Switching to \(sender.title)", ["model", sender.title]) }

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

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    let controller = Controller()
    func applicationDidFinishLaunching(_ note: Notification) { controller.start() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
