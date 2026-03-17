"""AutoEvolve — 让任何 AI Agent 自主进化的 Python 框架。

灵感来自 Karpathy 的 autoresearch，但通用化：不限于ML训练，
适用于任何「改一个变量→测量→保留/回滚」的场景。

核心设计（从 autoresearch 源码提取）：
1. 单一配置对象（AutoConfig dataclass）
2. 小模块组合（每个类 <50 行）
3. 进度调度（基于时间，非步数）
4. 快速失败（crash检测+自动回滚）
5. 配置自适应（根据资源调整）
6. 健康监控（v0.1.2：资源/停滞/崩溃检测）
7. 质量追踪（v0.1.2：趋势分析/自动调参）
"""
from __future__ import annotations
import json, os, time, subprocess, hashlib, statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable, List, Dict

__version__ = "0.1.2"

# ---------------------------------------------------------------------------
# 1. 单一配置对象（类比 GPTConfig）
# ---------------------------------------------------------------------------
@dataclass
class AutoConfig:
    """整个进化系统的配置。改这里 = 改系统行为。"""
    project_dir: str = "."           # 项目根目录
    strategy_file: str = "strategy.md"  # Agent 修改的唯一文件
    metrics_file: str = "results.tsv"   # 实验记录
    time_budget: int = 300           # 每次实验时间（秒）
    min_improvement: float = 0.01    # 最小改进阈值
    max_crash_streak: int = 3        # 最大连续崩溃数
    mode: str = "explore"            # explore | exploit | hybrid
    # v0.1.2 新增
    max_stall_cycles: int = 5        # 连续无改进后触发停滞警报
    quality_window: int = 10         # 质量评估窗口
    auto_adapt: bool = True          # 自动调整参数

# ---------------------------------------------------------------------------
# 2. 小模块：实验管理器（类比训练循环）
# ---------------------------------------------------------------------------
class Experiment:
    """一次实验：改代码→运行→测量→判断。"""
    
    def __init__(self, config: AutoConfig):
        self.config = config
        self.project = Path(config.project_dir)
        self.strategy_path = self.project / config.strategy_file
        self.results_path = self.project / config.metrics_file
    
    def read_strategy(self) -> str:
        """读取当前策略。"""
        return self.strategy_path.read_text() if self.strategy_path.exists() else ""
    
    def save_strategy(self, content: str) -> None:
        """保存策略变更。"""
        self.strategy_path.write_text(content)
    
    def run(self, command: str, timeout: Optional[int] = None) -> dict:
        """运行实验命令，返回结果。"""
        timeout = timeout or self.config.time_budget
        start = time.time()
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, 
                text=True, timeout=timeout, cwd=str(self.project)
            )
            elapsed = time.time() - start
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "elapsed": elapsed,
                "crashed": False
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False, "stdout": "", "stderr": "timeout",
                "elapsed": timeout, "crashed": True
            }
        except Exception as e:
            return {
                "success": False, "stdout": "", "stderr": str(e),
                "elapsed": time.time() - start, "crashed": True
            }
    
    def extract_metric(self, output: str, pattern: str = "score:") -> Optional[float]:
        """从输出中提取指标值。"""
        for line in output.split('\n'):
            if pattern in line:
                try:
                    return float(line.split(pattern)[1].strip().split()[0])
                except (IndexError, ValueError):
                    continue
        return None

# ---------------------------------------------------------------------------
# 3. 小模块：指标追踪器（类比 results.tsv）
# ---------------------------------------------------------------------------
class Tracker:
    """追踪实验历史，计算趋势。"""
    
    def __init__(self, results_path: str):
        self.path = Path(results_path)
        self._ensure_header()
    
    def _ensure_header(self):
        if not self.path.exists():
            self.path.write_text("cycle\ttime\tstrategy\tmetric\tstatus\tnotes\n")
    
    def record(self, cycle: int, strategy: str, metric: float, 
               status: str, notes: str = ""):
        """记录一次实验结果。"""
        t = time.strftime("%H:%M")
        with open(self.path, 'a') as f:
            f.write(f"{cycle}\t{t}\t{strategy}\t{metric:.4f}\t{status}\t{notes}\n")
    
    def last_score(self) -> float:
        """获取最近一次得分。"""
        lines = self.path.read_text().strip().split('\n')
        if len(lines) <= 1:
            return 0.0
        try:
            return float(lines[-1].split('\t')[3])
        except (IndexError, ValueError):
            return 0.0
    
    def trend(self, n: int = 5) -> str:
        """最近 N 次的趋势：up/down/stable。"""
        lines = self.path.read_text().strip().split('\n')[-n:]
        scores = []
        for line in lines:
            try:
                scores.append(float(line.split('\t')[3]))
            except (IndexError, ValueError):
                continue
        if len(scores) < 2:
            return "insufficient_data"
        if scores[-1] > scores[0] * 1.05:
            return "up"
        elif scores[-1] < scores[0] * 0.95:
            return "down"
        return "stable"
    
    def all_scores(self) -> List[float]:
        """获取所有得分。"""
        lines = self.path.read_text().strip().split('\n')
        scores = []
        for line in lines[1:]:  # skip header
            try:
                scores.append(float(line.split('\t')[3]))
            except (IndexError, ValueError):
                continue
        return scores
    
    def kept_count(self) -> int:
        """统计保留的改进次数。"""
        lines = self.path.read_text().strip().split('\n')
        count = 0
        for line in lines[1:]:
            parts = line.split('\t')
            if len(parts) >= 5 and parts[4] == "keep":
                count += 1
        return count

# ---------------------------------------------------------------------------
# 4. 小模块：回滚管理器（类比 git reset）
# ---------------------------------------------------------------------------
class Rollback:
    """策略文件的版本管理。"""
    
    def __init__(self, project_dir: str, strategy_file: str = "strategy.md"):
        self.project = Path(project_dir)
        self.strategy = self.project / strategy_file
        self.snapshots_dir = self.project / ".snapshots"
        self.snapshots_dir.mkdir(exist_ok=True)
    
    def snapshot(self, label: str = "") -> str:
        """保存当前策略快照。"""
        content = self.strategy.read_text() if self.strategy.exists() else ""
        h = hashlib.md5(content.encode()).hexdigest()[:8]
        name = f"{int(time.time())}_{h}_{label}.md"
        (self.snapshots_dir / name).write_text(content)
        return name
    
    def revert(self, snapshot_name: str) -> bool:
        """回滚到指定快照。"""
        snap = self.snapshots_dir / snapshot_name
        if snap.exists():
            self.strategy.write_text(snap.read_text())
            return True
        return False
    
    def latest(self) -> Optional[str]:
        """获取最新快照。"""
        snaps = sorted(self.snapshots_dir.glob("*.md"))
        return snaps[-1].name if snaps else None

# ---------------------------------------------------------------------------
# 5. 小模块：健康监控（v0.1.2 新增）
# ---------------------------------------------------------------------------
class HealthMonitor:
    """监控进化过程健康状态：崩溃、停滞、资源使用。"""
    
    def __init__(self, config: AutoConfig):
        self.config = config
        self.stall_count = 0
        self.total_crashes = 0
        self.start_time = time.time()
        self.cycle_times: List[float] = []
    
    def on_cycle_start(self):
        self._cycle_start = time.time()
    
    def on_cycle_end(self, result: dict):
        elapsed = time.time() - self._cycle_start
        self.cycle_times.append(elapsed)
        
        if result.get("status") in ("crash", "crash_limit"):
            self.total_crashes += 1
            self.stall_count = 0
        elif result.get("status") == "keep":
            self.stall_count = 0
        elif result.get("status") == "discard":
            self.stall_count += 1
    
    def is_stalled(self) -> bool:
        return self.stall_count >= self.config.max_stall_cycles
    
    def is_healthy(self) -> bool:
        if self.total_crashes >= self.config.max_crash_streak * 2:
            return False
        if self.is_stalled():
            return False
        return True
    
    def report(self) -> dict:
        uptime = time.time() - self.start_time
        avg_cycle = statistics.mean(self.cycle_times) if self.cycle_times else 0
        return {
            "uptime_sec": round(uptime),
            "total_cycles": len(self.cycle_times),
            "total_crashes": self.total_crashes,
            "stall_count": self.stall_count,
            "avg_cycle_sec": round(avg_cycle, 1),
            "is_stalled": self.is_stalled(),
            "is_healthy": self.is_healthy(),
        }
    
    def suggest_action(self) -> Optional[str]:
        """基于状态建议下一步行动。"""
        if self.is_stalled():
            return "stagnant: try increasing min_improvement or changing modify_fn strategy"
        if self.total_crashes >= self.config.max_crash_streak:
            return "too_many_crashes: check run_command and environment"
        if self.cycle_times and statistics.mean(self.cycle_times) > self.config.time_budget * 0.9:
            return "slow_cycles: consider reducing time_budget or optimizing experiment"
        return None

# ---------------------------------------------------------------------------
# 6. 小模块：质量追踪器（v0.1.2 新增）
# ---------------------------------------------------------------------------
class QualityTracker:
    """追踪进化质量：改进幅度、稳定性、衰减检测。"""
    
    def __init__(self, window: int = 10):
        self.window = window
        self.improvements: List[float] = []
        self.delta_history: List[float] = []
    
    def record_improvement(self, old_score: float, new_score: float, kept: bool):
        if kept:
            delta = new_score - old_score
            self.improvements.append(delta)
            self.delta_history.append(delta)
        else:
            self.delta_history.append(0.0)
    
    def avg_improvement(self) -> float:
        if not self.improvements:
            return 0.0
        recent = self.improvements[-self.window:]
        return statistics.mean(recent)
    
    def improvement_trend(self) -> str:
        """改进幅度趋势：加速/减速/稳定。"""
        if len(self.delta_history) < self.window:
            return "insufficient_data"
        half = self.window // 2
        first_half = self.delta_history[-self.window:-half]
        second_half = self.delta_history[-half:]
        if not first_half or not second_half:
            return "insufficient_data"
        avg_first = statistics.mean(first_half) if first_half else 0
        avg_second = statistics.mean(second_half) if second_half else 0
        if avg_second > avg_first * 1.1:
            return "accelerating"
        elif avg_second < avg_first * 0.9:
            return "decelerating"
        return "stable"
    
    def quality_score(self) -> float:
        """综合质量分 0-1：改进频率 × 平均幅度。"""
        if not self.delta_history:
            return 0.0
        recent = self.delta_history[-self.window:]
        frequency = sum(1 for d in recent if d > 0) / len(recent)
        magnitude = self.avg_improvement()
        return round(min(1.0, frequency * (1 + magnitude)), 3)
    
    def suggest_params(self, config: AutoConfig) -> dict:
        """基于质量趋势建议参数调整。"""
        suggestions = {}
        trend = self.improvement_trend()
        if trend == "decelerating":
            suggestions["min_improvement"] = max(0.001, config.min_improvement * 0.5)
            suggestions["reason"] = "improvements shrinking, lowering threshold"
        elif trend == "accelerating":
            suggestions["min_improvement"] = min(0.1, config.min_improvement * 1.5)
            suggestions["reason"] = "improvements growing, raising threshold for quality"
        return suggestions

# ---------------------------------------------------------------------------
# 7. 主循环（类比 autoresearch 的 LOOP FOREVER）
# ---------------------------------------------------------------------------
class Evolver:
    """进化主引擎。连接所有模块。"""
    
    def __init__(self, config: AutoConfig):
        self.config = config
        self.exp = Experiment(config)
        self.tracker = Tracker(str(Path(config.project_dir) / config.metrics_file))
        self.rollback = Rollback(config.project_dir, config.strategy_file)
        self.health = HealthMonitor(config)
        self.quality = QualityTracker(window=config.quality_window)
        self.cycle = 0
        self.crash_streak = 0
    
    def step(self, modify_fn: Callable, run_command: str, 
             metric_pattern: str = "score:") -> dict:
        """执行一个进化周期。"""
        self.cycle += 1
        self.health.on_cycle_start()
        
        # 1. 快照当前状态
        snap_name = self.rollback.snapshot(f"cycle{self.cycle}")
        
        # 2. 修改策略
        old_strategy = self.exp.read_strategy()
        new_strategy = modify_fn(old_strategy)
        self.exp.save_strategy(new_strategy)
        
        # 3. 运行实验
        result = self.exp.run(run_command)
        
        # 4. 崩溃处理
        if result["crashed"]:
            self.crash_streak += 1
            self.rollback.revert(snap_name)
            
            if self.crash_streak >= self.config.max_crash_streak:
                r = {
                    "cycle": self.cycle, "metric": 0, 
                    "status": "crash_limit", 
                    "notes": f"{self.crash_streak} consecutive crashes"
                }
                self.health.on_cycle_end(r)
                return r
            
            r = {
                "cycle": self.cycle, "metric": 0, 
                "status": "crash", "notes": result["stderr"][:50]
            }
            self.tracker.record(self.cycle, "crashed", 0.0, "crash", result["stderr"][:50])
            self.health.on_cycle_end(r)
            return r
        
        self.crash_streak = 0
        
        # 5. 提取指标
        metric = self.exp.extract_metric(result["stdout"], metric_pattern)
        if metric is None:
            metric = 0.0
        
        # 6. 判断保留/回退
        last = self.tracker.last_score()
        if metric > last + self.config.min_improvement:
            status = "keep"
            notes = f"improved {last:.4f} → {metric:.4f}"
            self.quality.record_improvement(last, metric, kept=True)
        else:
            self.rollback.revert(snap_name)
            status = "discard"
            notes = f"no improvement ({metric:.4f} vs {last:.4f})"
            self.quality.record_improvement(last, metric, kept=False)
        
        self.tracker.record(self.cycle, "modified", metric, status, notes)
        
        # 7. 自动调参（v0.1.2）
        if self.config.auto_adapt and self.cycle % self.config.quality_window == 0:
            suggestions = self.quality.suggest_params(self.config)
            if suggestions.get("min_improvement"):
                self.config.min_improvement = suggestions["min_improvement"]
        
        r = {
            "cycle": self.cycle, "metric": metric,
            "status": status, "notes": notes
        }
        self.health.on_cycle_end(r)
        return r
    
    def run_forever(self, modify_fn: Callable, run_command: str,
                    metric_pattern: str = "score:", 
                    max_cycles: int = 0) -> List[dict]:
        """运行进化循环直到手动停止或达到健康限制。
        
        Args:
            modify_fn: 策略修改函数
            run_command: 实验命令
            metric_pattern: 指标提取模式
            max_cycles: 最大周期数（0=无限）
        """
        results = []
        while True:
            r = self.step(modify_fn, run_command, metric_pattern)
            results.append(r)
            
            if r["status"] == "crash_limit":
                print(f"🛑 Crash limit reached at cycle {r['cycle']}")
                break
            if self.health.is_stalled():
                print(f"⏸️  Stalled at cycle {r['cycle']} ({self.health.stall_count} cycles no improvement)")
                suggestion = self.health.suggest_action()
                if suggestion:
                    print(f"   💡 {suggestion}")
                break
            if max_cycles > 0 and self.cycle >= max_cycles:
                print(f"✅ Completed {max_cycles} cycles")
                break
        
        return results
    
    def status(self) -> dict:
        """获取完整状态摘要。"""
        return {
            "version": __version__,
            "cycle": self.cycle,
            "config": asdict(self.config),
            "health": self.health.report(),
            "quality": {
                "avg_improvement": round(self.quality.avg_improvement(), 6),
                "trend": self.quality.improvement_trend(),
                "score": self.quality.quality_score(),
            },
            "tracker": {
                "last_score": self.tracker.last_score(),
                "trend": self.tracker.trend(),
                "kept": self.tracker.kept_count(),
                "total_scores": len(self.tracker.all_scores()),
            }
        }

# ---------------------------------------------------------------------------
# 快速启动
# ---------------------------------------------------------------------------
def quick_start(project_dir: str, modify_fn: Callable, 
                run_command: str, **kwargs) -> Evolver:
    """一键启动进化循环。"""
    config = AutoConfig(project_dir=project_dir, **kwargs)
    return Evolver(config)


def cli_main():
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(prog="autoevolve",
        description="Let any AI agent autonomously evolve through experimentation")
    parser.add_argument("--version", action="version", version=f"autoevolve {__version__}")
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run evolution cycles")
    run_p.add_argument("--project", default=".")
    run_p.add_argument("--command", required=True, help="Experiment command")
    run_p.add_argument("--metric", default="score:", help="Metric extraction pattern")
    run_p.add_argument("--cycles", type=int, default=1)
    run_p.add_argument("--budget", type=int, default=300)
    run_p.add_argument("--min-improvement", type=float, default=0.01)
    run_p.add_argument("--forever", action="store_true", help="Run until stalled or crash limit")

    res_p = sub.add_parser("results", help="Show results")
    res_p.add_argument("--project", default=".")
    res_p.add_argument("--last", type=int, default=10)

    trend_p = sub.add_parser("trend", help="Show trend")
    trend_p.add_argument("--project", default=".")
    trend_p.add_argument("--last", type=int, default=5)

    health_p = sub.add_parser("health", help="Show health status")
    health_p.add_argument("--project", default=".")

    args = parser.parse_args()

    if args.command == "run":
        config = AutoConfig(project_dir=args.project, time_budget=args.budget,
                           min_improvement=args.min_improvement)
        evolver = Evolver(config)
        def no_op(s): return s
        if args.forever:
            results = evolver.run_forever(no_op, args.command, args.metric, args.cycles or 0)
            print(f"\n📊 {len(results)} cycles | Quality: {evolver.quality.quality_score()} | Health: {evolver.health.report()}")
        else:
            for _ in range(args.cycles):
                r = evolver.step(no_op, args.command, args.metric)
                icon = "✅" if r["status"] == "keep" else "❌" if r["status"] == "crash" else "↩️"
                print(f"{icon} Cycle {r['cycle']}: {r['metric']:.4f} [{r['status']}] {r['notes']}")
            print(f"\nTrend: {evolver.tracker.trend(args.last)}")

    elif args.command == "results":
        p = Path(args.project) / "results.tsv"
        if not p.exists(): print("No results."); return
        for line in p.read_text().strip().split('\n')[-args.last:]:
            print(line)

    elif args.command == "trend":
        config = AutoConfig(project_dir=args.project)
        t = Tracker(str(Path(args.project) / config.metrics_file))
        print(f"Last: {t.last_score():.4f} | Trend: {t.trend(args.last)} | Kept: {t.kept_count()}")

    elif args.command == "health":
        # 从 results.tsv 重建健康状态
        config = AutoConfig(project_dir=args.project)
        t = Tracker(str(Path(args.project) / config.metrics_file))
        scores = t.all_scores()
        health = HealthMonitor(config)
        quality = QualityTracker()
        for i, s in enumerate(scores):
            prev = scores[i-1] if i > 0 else 0
            kept = s > prev + config.min_improvement
            quality.record_improvement(prev, s, kept)
            if kept:
                health.stall_count = 0
            else:
                health.stall_count += 1
        print(f"📊 autoevolve v{__version__}")
        print(f"   Cycles: {len(scores)} | Kept: {t.kept_count()}")
        print(f"   Last Score: {t.last_score():.4f} | Trend: {t.trend()}")
        print(f"   Avg Improvement: {quality.avg_improvement():.6f}")
        print(f"   Quality Score: {quality.quality_score()}")
        print(f"   Improvement Trend: {quality.improvement_trend()}")
        suggestion = health.suggest_action()
        if suggestion:
            print(f"   💡 Suggestion: {suggestion}")
        else:
            print(f"   ✅ Health: OK")

    else:
        parser.print_help()


if __name__ == "__main__":
    cli_main()
