import datetime
import sys
import typing as t
import pprint
from pathlib import Path
from collections import namedtuple

import pytablewriter
import numpy
import matplotlib
# Force matplotlib to not use any Xwindows backend.
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from .config import Target
from .benchmarks import Benchmark
from .logging import get_logger

logger = get_logger()


def format_val(bench_name, val):
    if 'mem-usage' in bench_name:
        return "%sMiB" % (int(val) / 1000.)
    else:
        return str(datetime.timedelta(seconds=float(val)))


def get_times_table(name_to_times_map):
    timestr = "\n"
    for name, times in sorted(name_to_times_map.items()):
        for time_ in times:
            timestr += "{0}: {1}\n".format(name, format_val(name, time_))

    return timestr


class BenchVal(namedtuple('BenchVal', 'name,values')):
    @property
    def count(self): return len(self.values)

    @property
    def avg(self): return sum(self.values) / self.count

    @property
    def stddev(self): return numpy.std(self.values)

    @property
    def summary_str(self):
        return "{0:.4f} (± {1:.4f})".format(self.avg, self.stddev)


class BenchList(t.List[Benchmark]):

    def total_time_result(self) -> BenchVal:
        return BenchVal(
            total_time_id(self[0].id),
            [i.results.total_time for i in self],
        )

    def peak_rss_result(self) -> t.Optional[BenchVal]:
        if not self[0].results.peak_rss_kb:
            # Some benchmarks don't track peak RSS
            return None

        return BenchVal(
            total_mem_id(self[0].id),
            [i.results.peak_rss_kb for i in self]
        )

    def microbench_result(self, microbench_name) -> t.Optional[BenchVal]:
        if not self[0].id.startswith('micro'):
            return None

        return BenchVal(
            total_time_id(microbench_name),
            [i.results.bench_to_time[microbench_name] for i in self],
        )


class GroupedRuns(t.Dict[str, t.Dict[Target, BenchList]]):

    @property
    def bench_ids(self) -> t.List[str]:
        return list(self.keys())

    @property
    def targets(self) -> t.List[Target]:
        benches_from_first: dict = self[list(self.keys())[0]]
        return list(benches_from_first.keys())

    @property
    def target_names(self) -> t.List[str]:
        return [i.name for i in self.targets]

    @classmethod
    def from_list(cls, in_list: [Benchmark]) -> 'GroupedRuns':
        """
        Group bench runs by benchmark.id -> Target -> [list of benches].

        Nota bene: this code is structured to take advantage of the fact that
        `in_list` is ordered by execution (earliest first).
        """
        out = cls()
        targets = []

        for bench in in_list:
            if bench.target not in targets:
                targets.append(bench.target)

        for bench in in_list:
            if bench.id not in out:
                out[bench.id] = {target: BenchList() for target in targets}

        for bench in in_list:
            out[bench.id][bench.target].append(bench)

        return out


def total_time_id(s: str) -> str:
    return s + '.total_secs'


def total_mem_id(s: str) -> str:
    return s + '.peak_rss_KiB'


# Keyed by bench run ID and valued by a list (in target execution order) of
# the bench results.
FlatResults = t.Dict[str, t.List[BenchVal]]


def get_standard_results(runs: GroupedRuns) -> FlatResults:
    """
    Return a view of the "standard" bench results, i.e. total time and memory
    usage per benchmark, plus all times for each micro bench.
    """
    out: FlatResults = dict()

    for bench_id, target_to_benchlist in runs.items():
        out[total_time_id(bench_id)] = [
            target_to_benchlist[t].total_time_result() for t in runs.targets]

        # Microbenches don't report memory
        if not bench_id.startswith('micro'):
            out[total_mem_id(bench_id)] = [
                target_to_benchlist[t].peak_rss_result() for t in runs.targets]

        if bench_id.startswith('micro'):
            # Enumerate out all microbench results
            first_bench = list(target_to_benchlist.values())[0][0]
            microbench_names = list(first_bench.results.bench_to_time.keys())

            compiler = first_bench.compiler
            for name in microbench_names:
                time_id = total_time_id(
                    'micro.{}.{}'.format(compiler, name))
                try:
                    out[time_id] = [
                        target_to_benchlist[t].microbench_result(name)
                        for t in runs.targets]
                except Exception:
                    logger.exception("Missing results for %s", name)

    # Remove rows without results
    for name in list(out.keys()):
        if any(not i or not list(filter(None, i.values))
               for i in out[name] if i):
            del out[name]

    return out


def print_comparative_times_table(cfg, runs: GroupedRuns, pfile=sys.stdout):
    output_path = cfg.results_dir / 'table.txt'
    outfile = open(output_path, 'w')
    print(file=outfile)
    writer = pytablewriter.MarkdownTableWriter()
    vs_str = ("{}" + (" vs. {}" * (len(runs.target_names) - 1))).format(
        *runs.target_names)
    writer.table_name = vs_str + " (absolute)"
    writer.header_list = ["bench name", "x", *runs.target_names]
    writer.value_matrix = []
    writer.margin = 1
    writer.stream = pfile

    std_results = get_standard_results(runs)

    for bench_id, result_list in std_results.items():
        if any(not r for r in result_list):
            # Skip this row; if we're missing anything we probably don't care
            # about the results.
            continue

        writer.value_matrix.append(
            [bench_id, result_list[0].count,
             *[r.summary_str if r else '-' for r in result_list]])

    writer.write_table()
    print(file=outfile)

    writer.table_name = vs_str + " (relative)"
    writer.value_matrix = []

    for bench_id, result_list in std_results.items():
        if any(not r for r in result_list):
            continue  # Skip this row; can't compare against nothing.

        minval = min([r.avg for r in result_list])
        normrow = [i.avg / minval for i in result_list]
        writer.value_matrix.append(
            [bench_id, result_list[0].count, *normrow])

    writer.write_table()
    outfile.close()
    print(output_path.read_text())


def make_plots(cfg, runs: GroupedRuns):
    """
    Generate matplotlib output based upon bench results.
    """
    output_path = cfg.results_dir / 'plots'
    output_path.mkdir(exist_ok=True)

    # Font size stuff lifted from https://stackoverflow.com/a/39566040
    SMALL_SIZE = 8
    MEDIUM_SIZE = 10
    BIGGER_SIZE = 12

    plt.rc('font', size=SMALL_SIZE)          # controls default text sizes
    plt.rc('axes', titlesize=SMALL_SIZE)     # fontsize of the axes title
    plt.rc('axes', labelsize=MEDIUM_SIZE)    # fontsize of the x and y labels
    plt.rc('xtick', labelsize=SMALL_SIZE)    # fontsize of the tick labels
    plt.rc('ytick', labelsize=SMALL_SIZE)    # fontsize of the tick labels
    plt.rc('legend', fontsize=SMALL_SIZE)    # legend fontsize
    plt.rc('figure', titlesize=BIGGER_SIZE)  # fontsize of the figure title
    plt.rcParams.update({'font.family': 'monospace'})

    ibd_reindex_ids = [
        i for i in runs.bench_ids if
        i.startswith('ibd') or i.startswith('reindex')]

    plots_created = []

    for bench_id in ibd_reindex_ids:
        plots_created.append(_make_ibd_type_plot(
            cfg, bench_id, runs[bench_id]))

    if any(i.startswith('micro.') for i in runs.bench_ids):
        plots_created.append(_make_microbench_plot(cfg, runs))

    print("Generated plots:")
    pprint.pprint(plots_created)


def kib_to_mb(kib) -> float:
    return kib * 0.001024


def get_processor_info() -> str:
    cpuinfo_path = Path('/proc/cpuinfo')

    if not cpuinfo_path.exists():
        logger.warning("Couldn't detect CPU info")
        return ''

    lines = cpuinfo_path.read_text().splitlines()
    modelname = [
        i for i in lines if i.startswith('model name')][0].split(': ')[-1]
    return modelname.strip()


def get_git_info(benches) -> str:
    out = '\n'
    for bench in benches:
        out += '{}: {}\n'.format(bench.target.name, bench.gitco.sha)
    return out.rstrip()


def _make_ibd_type_plot(
    cfg, bench_id: str, run_data: t.Dict[Target, BenchList]) \
        -> Path:
    plt.clf()
    output_path = cfg.results_dir / 'plots'

    # The number of runs we did dictates how many IBD charts we should
    # generate.
    num_runs = len(list(run_data.values())[0])

    f, axis_pairs = plt.subplots(1 + num_runs, 2)
    f.set_size_inches(8, 8)

    # The first two axes will be used for aggregate statistics, the following
    # axes pairs will be used for height/mem profiles.
    ax1, ax2 = axis_pairs[0]

    total_time_data = []
    peak_mem_data = []

    targets = [target for target in run_data.keys()]
    target_names = [target.name for target in targets]

    # Benchlist per target in canonical order (due to sorted-by-default dicts)
    for target, benchlist in zip(targets, run_data.values()):
        # Ensure the data ordering is what we expect.
        assert run_data[target] == benchlist

        total_time_data.append([b.results.total_time for b in benchlist])
        peak_mem_data.append(
            [kib_to_mb(b.results.peak_rss_kb) for b in benchlist])


    def add_iters(b, add=''):
        return b + add + " (x{})".format(num_runs)

    ax1.boxplot(total_time_data)
    ax1.set_title(add_iters(bench_id))
    ax1.set_xticklabels(target_names)
    ax1.set(ylabel='Seconds')

    ax2.boxplot(peak_mem_data)
    ax2.set_title('Peak memory usage')
    ax2.set_xticklabels([i[:22] for i in target_names])
    ax2.set(ylabel='MB')

    for i, axis_pair in enumerate(axis_pairs[1:]):
        height_ax, mem_ax = axis_pair
        # Construct profile graphs for each separate benchmark run.

        height_ax.set_title('Height at time (run {})'.format(i))
        mem_ax.set_title('Memory usage at time (run {})'.format(i))

        for target, benchlist in zip(targets, run_data.values()):
            assert run_data[target] == benchlist

            # For axis_pair i, we only care about the i'th bench result.
            results = benchlist[i].results

            timeseries_height_data = []
            timeseries_mem_data = []
            timeseries_fds_data = []
            timeseries_cpu_data = []

            for height, hdata in results.height_to_data.items():
                timeseries_height_data.append((hdata.time_secs, height))
                timeseries_mem_data.append(
                    (hdata.time_secs, kib_to_mb(hdata.rss_kb)))
                timeseries_fds_data.append((hdata.time_secs, hdata.num_fds))
                timeseries_cpu_data.append(
                    (hdata.time_secs, hdata.cpu_percentage))

            x = [i[0] for i in timeseries_height_data]
            y = [i[1] for i in timeseries_height_data]
            height_ax.plot(x, y, label=target.name)

            x = [i[0] for i in timeseries_mem_data]
            y = [i[1] for i in timeseries_mem_data]
            mem_ax.plot(x, y, label=target.name)

        height_ax.set(ylabel='Height')
        height_ax.legend()

        mem_ax.set(ylabel='MB')
        mem_ax.legend()

    plt.tight_layout(rect=[0, 0.09, 1, 1])
    txt = plt.figtext(
        0.25, 0.02,
        "Benchmarks performed on {}{}".format(
            get_processor_info(),
            get_git_info([i[0] for i in run_data.values()])))
    txt.set_fontfamily('sans-serif')

    plot_path = "{}/{}.png".format(output_path, bench_id)
    plt.savefig(plot_path)
    return Path(plot_path)


def _make_microbench_plot(cfg, runs) -> Path:
    plt.clf()
    output_path = cfg.results_dir / 'plots'
    micro_key = [i for i in runs.keys() if i.startswith('micro')][0]
    target_names = [target.name for target in runs[micro_key].keys()]

    std: FlatResults = get_standard_results(runs)
    micro_names = [
        i for i in std.keys() if i.startswith('micro.') and '.j=' not in i]

    # Difference in results must beat this percentage to be displayed.
    interesting_threshold = 0.03

    significant_micro_names = [
        i for i in micro_names if
        _find_max_diff(std[i]) >= interesting_threshold
    ]
    f, axes = plt.subplots(1, len(significant_micro_names))

    try:
        axes[0]
    except TypeError:
        axes = (axes,)

    for i, name in enumerate(significant_micro_names):
        ax = axes[i]

        ax.boxplot([i.values for i in std[name]])
        ax.set_title("{} (x{})".format(name, std[name][0].count))
        ax.set_xticklabels(target_names)
        ax.set(ylabel='Seconds')

    plt.tight_layout(rect=[0, 0.14, 1, 1])
    txt = plt.figtext(
        0.2, 0.02,
        "Benchmarks performed on {}{}".format(
            get_processor_info(),
            get_git_info([i[0] for i in list(runs.values())[0].values()])))
    txt.set_fontfamily('sans-serif')

    plot_path = "{}/{}.png".format(output_path, 'microbenches')
    plt.savefig(plot_path)
    return Path(plot_path)


def _find_max_diff(benchval_list: t.List[BenchVal]) -> float:
    """
    Return the largest deviation from a list of aggregate bench values.
    """
    min_ = min(i.avg for i in benchval_list)
    return max(i.avg / min_ for i in benchval_list) - 1.
