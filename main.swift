// ClaudeSpeed — 菜单栏显示 Claude Code 最新回复的生成速度。
// 每 3s 调一次 collect.py(扫 ~/.claude/projects 的会话日志),
// 首行作标题,其余行进下拉菜单。零第三方依赖,swiftc 直接编译。
import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var timer: Timer?
    var running = false
    // collect.py 与二进制同目录:仓库 clone 到哪都能跑,无硬编码路径
    let script = URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0])
        .resolvingSymlinksInPath().deletingLastPathComponent()
        .appendingPathComponent("collect.py").path

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // 等宽数字:3s 刷新时数值变化不再引起标题宽度抖动。
        // 字号取菜单栏默认(14pt),systemFontSize 是 13pt 会比相邻图标小一号
        statusItem.button?.font = NSFont.monospacedDigitSystemFont(
            ofSize: NSFont.menuBarFont(ofSize: 0).pointSize, weight: .regular)
        statusItem.button?.title = "⚪"
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
    }

    func refresh() {
        if running { return }  // 上一轮没跑完就跳过,避免堆积
        running = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let process = Process()
            // 经 env 找 python3,兼容 Homebrew/pyenv 等非系统安装
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", self.script]
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            var output = ""
            do {
                try process.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                output = String(data: data, encoding: .utf8) ?? ""
            } catch {
                output = "⚡?"
            }
            let lines = output.split(separator: "\n").map(String.init)
            DispatchQueue.main.async {
                self.statusItem.button?.title = lines.first ?? "⚡?"
                let menu = NSMenu()
                // action:nil 的菜单项会被 AppKit 自动置灰;关掉自动使能,信息行按
                // 全对比度渲染(仍不可点),数字用等宽字体保证多行纵向对齐
                menu.autoenablesItems = false
                for line in lines.dropFirst() {
                    let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
                    item.attributedTitle = NSAttributedString(
                        string: line,
                        attributes: [.font: NSFont.monospacedDigitSystemFont(
                            ofSize: NSFont.menuFont(ofSize: 0).pointSize,
                            weight: .regular)])
                    item.isEnabled = true
                    menu.addItem(item)
                }
                menu.addItem(.separator())
                menu.addItem(NSMenuItem(
                    title: "Quit ClaudeSpeed",
                    action: #selector(NSApplication.terminate(_:)),
                    keyEquivalent: "q"
                ))
                self.statusItem.menu = menu
                self.running = false
            }
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)  // 不占 Dock,只驻菜单栏
let delegate = AppDelegate()
app.delegate = delegate
app.run()
