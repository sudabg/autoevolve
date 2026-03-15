# AutoEvolve

**Let any AI agent autonomously evolve through experimentation.**

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch), but generalized beyond ML training. Works for any scenario where you can measure improvement: code optimization, content generation, strategy tuning, hyperparameter search, A/B testing.

## Install

```bash
pip install autoevolve
```

## Quick Start

```python
from autoevolve import AutoConfig, Evolver

# Configure
config = AutoConfig(
    project_dir="./my_project",
    strategy_file="strategy.md",  # file the agent modifies
    time_budget=300,              # 5 minutes per experiment
    min_improvement=0.01,         # minimum improvement to keep
)

# Create evolver
evolver = Evolver(config)

# Define how to modify strategy
def increase_lr(strategy):
    return strategy.replace("lr=0.01", "lr=0.02")

# Run evolution loop
for _ in range(10):
    result = evolver.step(
        modify_fn=increase_lr,
        run_command="python train.py",
        metric_pattern="val_bpb:"  # extract from output
    )
    print(f"Cycle {result['cycle']}: {result['status']} ({result['notes']})")
```

## Core Concepts

### The Experiment Loop

```
1. Snapshot current strategy (git-like checkpoint)
2. Agent modifies the strategy file
3. Run the experiment (any command)
4. Extract metric from output
5. Compare to last result:
   - Better? Keep the change.
   - Worse? Rollback.
6. Record to results.tsv
7. Repeat forever (until human stops you)
```

### Components

| Component | Purpose | Lines |
|-----------|---------|-------|
| `AutoConfig` | Single config object (like GPTConfig) | 15 |
| `Experiment` | Run commands, extract metrics | 55 |
| `Tracker` | Results history, trend analysis | 45 |
| `Rollback` | Checkpoint/restore strategy file | 35 |
| `Evolver` | Main engine, connects everything | 55 |

**Total: ~250 lines of Python.** Simple enough to read in 10 minutes.

### Design Principles (from autoresearch source)

1. **Single Config Object** — All settings in one `@dataclass`. Change behavior by changing config.
2. **Small Modules** — Each class does ONE thing, under 60 lines.
3. **Time-based Scheduling** — Progress measured by time ratio, not step count.
4. **Fast Fail** — NaN/crash detected immediately, auto-rollback.
5. **Auto-scaling** — Adapts to available resources.

## CLI Usage

```bash
# Run a single evolution cycle
autoevolve run --project ./my_project --command "python train.py" --metric "score:"

# View results
autoevolve results --project ./my_project

# Check trend
autoevolve trend --project ./my_project --last 5
```

## Examples

### ML Hyperparameter Tuning

```python
def tune_hyperparams(strategy):
    import random
    lr = 0.01 * (1 + random.uniform(-0.5, 0.5))
    return strategy.replace(f"lr={old_lr}", f"lr={lr:.4f}")

result = evolver.step(tune_hyperparams, "python train.py", "val_loss:")
```

### Content Optimization

```python
def add_examples(strategy):
    return strategy + "\n## Examples\n..."

result = evolver.step(add_examples, "python evaluate.py", "quality_score:")
```

### Code Performance

```python
def try_optimization(strategy):
    return strategy.replace("# TODO: optimize", "result = np.dot(a, b)")

result = evolver.step(try_optimization, "python benchmark.py", "ops/sec:")
```

## Comparison with autoresearch

| Aspect | autoresearch | AutoEvolve |
|--------|-------------|------------|
| Scope | ML training only | Any measurable task |
| Code | 630 lines | 250 lines |
| Metric | val_bpb (fixed) | Configurable pattern |
| Rollback | git | File snapshots |
| Scheduling | Time-based | Time-based (inherited) |
| Crashes | exit(1) | Auto-rollback |

## License

MIT

## Acknowledgments

Built with lessons from [Karpathy's autoresearch](https://github.com/karpathy/autoresearch). Thank you for showing the way.
