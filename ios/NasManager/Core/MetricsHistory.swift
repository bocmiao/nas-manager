import Foundation
import Observation

/// 本地历史采样。
///
/// 每次成功刷新记一个点，同时保存 CPU/内存/温度/风扇/网速，
/// 图表按指标各取所需。环形缓冲最多 360 点，落盘到 UserDefaults（JSON）。
@Observable
final class MetricsHistory {
    struct Sample: Codable {
        let date: Date
        let cpu: Double?
        let mem: Double?
        let cpuTemp: Double?
        let cpuFan: Double?
        let deviceFan: Double?
        let sendRate: Double?
        let recvRate: Double?
    }

    /// 图表用的点。id 用切片内序号，保证同一序列内稳定。
    struct Point: Identifiable, Hashable {
        let id: Int
        let date: Date
        let value: Double
    }

    static let capacity = 360

    private static let storageKey = "metrics.samples.v1"
    /// 单次写入上限，异常数据时不至于把 UserDefaults 撑大
    private static let maxPayloadBytes = 200_000

    private(set) var samples: [Sample] = []

    init() {
        // init 里只读静态常量和 UserDefaults，不碰 self 的其他属性
        let defaults = UserDefaults.standard
        guard let data = defaults.data(forKey: Self.storageKey),
              let decoded = try? JSONDecoder().decode([Sample].self, from: data) else {
            return
        }
        samples = Array(decoded.suffix(Self.capacity))
    }

    var isEmpty: Bool { samples.isEmpty }

    // MARK: 采样

    func record(live: LiveOverview, date: Date = Date()) {
        let sample = Sample(
            date: date,
            cpu: live.cpuUsage,
            mem: live.memUsage,
            cpuTemp: live.cpuTemp.map { Double($0) },
            cpuFan: live.cpuFanSpeed.map { Double($0) },
            deviceFan: live.deviceFanSpeed.map { Double($0) },
            sendRate: live.sendRate.map { Double($0) },
            recvRate: live.recvRate.map { Double($0) }
        )

        // 全空就不记，免得曲线里出现断点
        guard sample.cpu != nil || sample.mem != nil || sample.cpuTemp != nil
            || sample.cpuFan != nil || sample.deviceFan != nil
            || sample.sendRate != nil || sample.recvRate != nil else { return }

        // 手动下拉刷新可能连点，5 秒内的重复采样直接丢弃
        if let last = samples.last, date.timeIntervalSince(last.date) < 5 { return }

        samples.append(sample)
        if samples.count > Self.capacity {
            samples.removeFirst(samples.count - Self.capacity)
        }
        save()
    }

    func clear() {
        samples.removeAll()
        UserDefaults.standard.removeObject(forKey: Self.storageKey)
    }

    // MARK: 取点

    func cpuPoints(limit: Int) -> [Point] {
        points(limit: limit) { $0.cpu }
    }

    func memoryPoints(limit: Int) -> [Point] {
        points(limit: limit) { $0.mem }
    }

    func temperaturePoints(limit: Int) -> [Point] {
        points(limit: limit) { $0.cpuTemp }
    }

    /// 网速换算成 MB/s，避免图表纵轴是十几个 G 的字节数
    func sendPoints(limit: Int) -> [Point] {
        points(limit: limit) { sample in
            sample.sendRate.map { $0 / 1_048_576 }
        }
    }

    func recvPoints(limit: Int) -> [Point] {
        points(limit: limit) { sample in
            sample.recvRate.map { $0 / 1_048_576 }
        }
    }

    private func points(limit: Int, value: (Sample) -> Double?) -> [Point] {
        let slice = samples.suffix(max(1, limit))
        var result: [Point] = []
        result.reserveCapacity(slice.count)
        for (offset, sample) in slice.enumerated() {
            guard let number = value(sample) else { continue }
            result.append(Point(id: offset, date: sample.date, value: number))
        }
        return result
    }

    // MARK: 持久化

    private func save() {
        guard let data = try? JSONEncoder().encode(samples) else { return }

        if data.count > Self.maxPayloadBytes {
            samples = Array(samples.suffix(120))
            guard let trimmed = try? JSONEncoder().encode(samples) else { return }
            UserDefaults.standard.set(trimmed, forKey: Self.storageKey)
            return
        }
        UserDefaults.standard.set(data, forKey: Self.storageKey)
    }
}
