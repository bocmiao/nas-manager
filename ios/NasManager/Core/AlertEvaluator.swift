import Foundation

/// 一条待写入事件中心的告警
struct AlertFinding {
    let key: String
    let title: String
    let detail: String
    let severity: AlertSeverity
}

/// 本地告警规则引擎。
///
/// 采用「边沿触发」：条件从正常变异常时产出一次，恢复后重新武装，避免每轮轮询都刷一条。
struct AlertEvaluator {
    /// 当前处于异常状态的标记（同一条件只报一次）
    private var firedTags: Set<String> = []
    /// 容器上一轮运行状态，用于识别「运行中 → 停止」
    private var containerStates: [String: Bool] = [:]
    private var seeded = false

    mutating func evaluate(dashboard: Dashboard) -> [AlertFinding] {
        var findings: [AlertFinding] = []

        threshold(&findings, rule: .cpuUsage, value: dashboard.live?.cpuUsage, tag: "cpuUsage", subject: "CPU 使用率", severity: .warning)
        threshold(&findings, rule: .memoryUsage, value: dashboard.live?.memUsage, tag: "memoryUsage", subject: "内存使用率", severity: .warning)

        if let temperature = dashboard.live?.cpuTemp {
            threshold(&findings, rule: .cpuTemperature, value: Double(temperature), tag: "cpuTemperature", subject: "CPU 温度", severity: .warning)
        }

        diskTemperatures(&findings, disks: dashboard.disks)
        volumeUsages(&findings, volumes: dashboard.volumes)
        poolHealth(&findings, pools: dashboard.pools)
        diskHealth(&findings, disks: dashboard.disks)
        containers(&findings, containers: dashboard.containers)

        seeded = true
        return findings
    }

    // MARK: 各规则

    private mutating func threshold(
        _ findings: inout [AlertFinding],
        rule: AlertRuleKey,
        value: Double?,
        tag: String,
        subject: String,
        severity: AlertSeverity
    ) {
        guard AlertRuleDefaults.isEnabled(rule), let value else { return }
        let limit = AlertRuleDefaults.threshold(rule)

        if value > limit {
            guard !firedTags.contains(tag) else { return }
            firedTags.insert(tag)
            findings.append(
                AlertFinding(
                    key: tag,
                    title: "\(subject)超过阈值",
                    detail: String(format: "%@ 当前 %.1f%@，阈值 %.0f%@", subject, value, rule.unit, limit, rule.unit),
                    severity: severity
                )
            )
        } else {
            firedTags.remove(tag)
        }
    }

    private mutating func diskTemperatures(_ findings: inout [AlertFinding], disks: [Disk]) {
        let rule = AlertRuleKey.diskTemperature
        guard AlertRuleDefaults.isEnabled(rule) else { return }
        let limit = AlertRuleDefaults.threshold(rule)

        for disk in disks {
            guard let temperature = disk.temperature else { continue }
            let tag = "diskTemperature:" + disk.id
            if Double(temperature) > limit {
                guard !firedTags.contains(tag) else { continue }
                firedTags.insert(tag)
                findings.append(
                    AlertFinding(
                        key: tag,
                        title: "磁盘温度过高",
                        detail: String(
                            format: "%@ 当前 %ld°C，阈值 %.0f°C",
                            disk.label ?? disk.name ?? "磁盘",
                            temperature,
                            limit
                        ),
                        severity: .warning
                    )
                )
            } else {
                firedTags.remove(tag)
            }
        }
    }

    private mutating func volumeUsages(_ findings: inout [AlertFinding], volumes: [Volume]) {
        let rule = AlertRuleKey.volumeUsage
        guard AlertRuleDefaults.isEnabled(rule) else { return }
        let limit = AlertRuleDefaults.threshold(rule)

        for volume in volumes {
            let percent = volume.usedPercent
            guard percent > 0 else { continue }
            let tag = "volumeUsage:" + volume.id
            if percent > limit {
                guard !firedTags.contains(tag) else { continue }
                firedTags.insert(tag)
                findings.append(
                    AlertFinding(
                        key: tag,
                        title: "存储卷空间不足",
                        detail: String(
                            format: "%@ 已用 %.0f%%，阈值 %.0f%%",
                            volume.label ?? volume.name ?? "卷",
                            percent,
                            limit
                        ),
                        severity: .warning
                    )
                )
            } else {
                firedTags.remove(tag)
            }
        }
    }

    private mutating func poolHealth(_ findings: inout [AlertFinding], pools: [Pool]) {
        let rule = AlertRuleKey.poolHealth
        guard AlertRuleDefaults.isEnabled(rule) else { return }

        var abnormal: Set<String> = []
        for pool in pools where pool.status != nil && !pool.isHealthy {
            let tag = "poolHealth:" + pool.id
            abnormal.insert(tag)
            guard !firedTags.contains(tag) else { continue }
            firedTags.insert(tag)
            findings.append(
                AlertFinding(
                    key: tag,
                    title: "存储池状态异常",
                    detail: "\(pool.label ?? pool.name ?? "存储池") 状态码 \(pool.status ?? -1)，请尽快检查",
                    severity: .critical
                )
            )
        }
        clearTags(prefix: "poolHealth:", keeping: abnormal)
    }

    private mutating func diskHealth(_ findings: inout [AlertFinding], disks: [Disk]) {
        let rule = AlertRuleKey.diskHealth
        guard AlertRuleDefaults.isEnabled(rule) else { return }

        var abnormal: Set<String> = []
        for disk in disks where disk.status != nil && !disk.isHealthy {
            let tag = "diskHealth:" + disk.id
            abnormal.insert(tag)
            guard !firedTags.contains(tag) else { continue }
            firedTags.insert(tag)
            findings.append(
                AlertFinding(
                    key: tag,
                    title: "磁盘状态异常",
                    detail: "\(disk.label ?? disk.name ?? "磁盘") 状态码 \(disk.status ?? -1)，可能已掉盘或故障",
                    severity: .critical
                )
            )
        }
        clearTags(prefix: "diskHealth:", keeping: abnormal)
    }

    private mutating func containers(_ findings: inout [AlertFinding], containers: [Container]) {
        let rule = AlertRuleKey.containerDown
        guard AlertRuleDefaults.isEnabled(rule) else { return }

        var current: [String: Bool] = [:]
        var names: [String: String] = [:]
        for container in containers {
            current[container.id] = container.isRunning
            names[container.id] = container.name ?? container.containerId ?? "容器"
        }

        if seeded && !containerStates.isEmpty {
            for (id, running) in current {
                guard let wasRunning = containerStates[id] else { continue }
                if wasRunning && !running {
                    findings.append(
                        AlertFinding(
                            key: "containerDown:" + id,
                            title: "容器已停止",
                            detail: "\(names[id] ?? id) 从运行状态变为停止",
                            severity: .critical
                        )
                    )
                }
            }
        }

        containerStates = current
    }

    private mutating func clearTags(prefix: String, keeping keep: Set<String>) {
        let stale = firedTags.filter { $0.hasPrefix(prefix) && !keep.contains($0) }
        for tag in stale {
            firedTags.remove(tag)
        }
    }
}
