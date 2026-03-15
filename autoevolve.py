"""AutoEvolve — 让任何 AI Agent 自主进化的 Python 框架。

灵感来自 Karpathy 的 autoresearch，但通用化：不限于ML训练，
适用于任何「改一个变量→测量→保留/回滚」的场景。

核心设计（从 autoresearch 源码提取）：
1. 单一配置对象（AutoConfig dataclass）
2. 小模块组合（每个类 <50 行）
3. 进度调度（基于时间，非步数）
4. 快速失败（crash检测+自动回滚）
5. 配置自适应（根据资源调整）
"""
from __future__ import annotations
import json, os, time, subprocess, hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable

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
# 5. 主循环（类比 autoresearch 的 LOOP FOREVER）
# ---------------------------------------------------------------------------
class Evolver:
    """进化主引擎。连接所有模块。"""
    
    def __init__(self, config: AutoConfig):
        self.config = config
        self.exp = Experiment(config)
        self.tracker = Tracker(str(Path(config.project_dir) / config.metrics_file))
        self.rollback = Rollback(config.project_dir, config.strategy_file)
        self.cycle = 0
        self.crash_streak = 0
    
    def step(self, modify_fn: Callable, run_command: str, 
             metric_pattern: str = "score:") -> dict:
        """执行一个进化周期。
        
        Args:
            modify_fn: 接收当前策略内容，返回修改后的策略内容
            run_command: 运行实验的 shell 命令
            metric_pattern: 从输出中提取指标的模式
            
        Returns:
            包含 cycle, metric, status, notes 的字典
        """
        self.cycle += 1
        
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
                return {
                    "cycle": self.cycle, "metric": 0, 
                    "status": "crash_limit", 
                    "notes": f"{self.crash_streak} consecutive crashes"
                }
            
            self.tracker.record(self.cycle, "crashed", 0.0, "crash", result["stderr"][:50])
            return {
                "cycle": self.cycle, "metric": 0, 
                "status": "crash", "notes": result["stderr"][:50]
            }
        
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
        else:
            self.rollback.revert(snap_name)
            status = "discard"
            notes = f"no improvement ({metric:.4f} vs {last:.4f})"
        
        self.tracker.record(self.cycle, "modified", metric, status, notes)
        
        return {
            "cycle": self.cycle, "metric": metric,
            "status": status, "notes": notes
        }

# ---------------------------------------------------------------------------
# 快速启动
# ---------------------------------------------------------------------------
def quick_start(project_dir: str, modify_fn: Callable, 
                run_command: str, **kwargs) -> Evolver:
    """一键启动进化循环。"""
    config = AutoConfig(project_dir=project_dir, **kwargs)
    return Evolver(config)

if __name__ == "__main__":
    # Demo: 进化一个简单的策略文件
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建示例策略
        Path(tmpdir, "strategy.md").write_text("# Strategy v1\nlr=0.01\n")
        
        # 创建示例运行脚本
        Path(tmpdir, "run.sh").write_text('''#!/bin/bash
# 模拟实验：输出随机分数
echo "score: $(( RANDOM % 100 + 50 )).$(( RANDOM % 100 ))"
''')
        os.chmod(Path(tmpdir, "run.sh"), 0o755)
        
        config = AutoConfig(project_dir=tmpdir, time_budget=10)
        evolver = Evolver(config)
        
        def double_lr(strategy):
            return strategy.replace("lr=0.01", "lr=0.02")
        
        # 运行 3 个周期
        for _ in range(3):
            result = evolver.step(double_lr, "bash run.sh", "score:")
            print(f"Cycle {result['cycle']}: {result['status']} ({result['notes']})")
        
        print(f"\nTrend: {evolver.tracker.trend()}")
        print(f"Results: {Path(tmpdir, 'results.tsv').read_text()}")
