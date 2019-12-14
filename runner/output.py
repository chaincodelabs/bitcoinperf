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

from .config import Config, Target
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

    @property
    def command(self) -> str:
        """Get the command string associated with the first bench instance."""
        return self.bench.results.command

    @property
    def bench(self) -> Benchmark:
        """
        Return the first benchmark instance. Should be characteristic,
        and allow for looking up metadata like start height, bench_cfg params,
        etc.
        """
        return self[0]

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


def _condense_bitcoin_cmd(cmd_in: str) -> str:
    out_str = ''
    ignore_list = (
        '-rpcport=', '-port=', '-datadir=', '-maxtipage=',
        '-minimumchainwork=')

    tokens = cmd_in.split()

    # Always put assumevalid at the back since it's really long.
    tokens.sort(key=lambda i: 'assumevalid' in i)

    for i in tokens:
        if i.endswith('/bitcoind'):
            out_str += 'bitcoind '
        elif i.startswith(ignore_list):
            # Skip - no need to display these.
            continue
        else:
            out_str += f"{i} "

    return out_str.strip()


def print_comparative_times_table(cfg, runs: GroupedRuns, pfile=sys.stdout):
    writer = pytablewriter.MarkdownTableWriter()
    vs_str = ("{}" + (" vs. {}" * (len(runs.target_names) - 1))).format(
        *runs.target_names)
    writer.header_list = ["bench name", "x", *runs.target_names]
    writer.value_matrix = []
    writer.margin = 1
    writer.stream = pfile

    std_results = get_standard_results(runs)
    important_commands = {}

    for bench_name, target_to_benchlist in runs.items():
        if 'ibd' in bench_name or 'reindex' in bench_name:
            important_commands[bench_name] = _condense_bitcoin_cmd(
                list(target_to_benchlist.items())[0][1].command)

    cmd_writer = pytablewriter.MarkdownTableWriter()
    cmd_writer.header_list = ["bench name", "command"]
    cmd_writer.value_matrix = []
    cmd_writer.margin = 1
    cmd_writer.stream = pfile

    for bench_name, command in important_commands.items():
        cmd_writer.value_matrix.append((bench_name, f"`{command}`"))

    out0 = '### commands index\n'
    out0 += cmd_writer.dumps() + '\n\n'
    print(out0)

    for bench_id, result_list in std_results.items():
        if any(not r for r in result_list):
            # Skip this row; if we're missing anything we probably don't care
            # about the results.
            continue

        writer.value_matrix.append(
            [bench_id, result_list[0].count,
             *[r.summary_str if r else '-' for r in result_list]])

    out = '### ' + vs_str + " (absolute)\n"
    out += writer.dumps() + '\n\n'
    print(out)

    writer.value_matrix = []

    for bench_id, result_list in std_results.items():
        if any(not r for r in result_list):
            continue  # Skip this row; can't compare against nothing.

        minval = min([r.avg for r in result_list])
        normrow = [i.avg / minval for i in result_list]
        writer.value_matrix.append(
            [bench_id, result_list[0].count, *normrow])

    out2 = '### ' + vs_str + " (relative)\n"
    out2 += writer.dumps() + '\n\n'
    print(out2)
    output_path = cfg.results_dir / 'table.txt'
    output_path.write_text(out0 + out + out2)
    print()
    print(f"This output has been written to {output_path}")


def make_plots(cfg: Config, runs: GroupedRuns):
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
        plots_created.append(_make_cache_flush_plot(
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


def _get_git_info(benches) -> str:
    out = '\n'
    for bench in benches:
        out += '{}: {}\n'.format(bench.target.name, bench.gitco.sha[:10])
    return out.rstrip()


def _format_command(cmd: str) -> str:
    char_count = 0
    MAX_LEN = 70
    out = ''

    for i in _condense_bitcoin_cmd(cmd).split():
        if 'assumevalid' in i:
            out += f'\n    {i[:50]}...\n'
            char_count = 0
            continue

        char_count += len(i)

        if char_count >= MAX_LEN or 'assumevalid' in i:
            out += '\n    '
            char_count = 0

        out += f'{i} '

    return out


def _get_dbcache(cmd: str) -> str:
    for i in cmd.split():
        if i.startswith('-dbcache='):
            return i.split('=')[-1]


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

    # Get the plot title from the first bench command we see.
    cmd_str: str = ''
    title: str = ''

    # Benchlist per target in canonical order (due to sorted-by-default dicts)
    for target, benchlist in zip(targets, run_data.values()):
        # Ensure the data ordering is what we expect.
        assert run_data[target] == benchlist

        if not cmd_str:
            cmd_str = _format_command(benchlist[0].results.command)
            print(benchlist[0].results.configure_info)

        if not title:
            title = benchlist[0].results.title

        total_time_data.append([b.results.total_time for b in benchlist])
        peak_mem_data.append(
            [kib_to_mb(b.results.peak_rss_kb) for b in benchlist])


    def add_iters(b, add=''):
        return b + add + " (x{})".format(num_runs)

    dbcache = _get_dbcache(cmd_str)

    f.suptitle(title, fontsize=12, fontfamily='sanserif')

    ax1.boxplot(total_time_data)
    ax1.set_title(add_iters(bench_id))
    ax1.set_xticklabels(target_names)
    ax1.set(ylabel='Seconds')

    ax2.set_title('Memory')
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

    plt.tight_layout(rect=[0, 0.1, 1, 0.92])
    txt = plt.figtext(
        0.6, 0.02,
        "Benchmarks performed on\n{}{}".format(
            get_processor_info(),
            _get_git_info([i[0] for i in run_data.values()])))
    txt.set_fontfamily('sans-serif')

    txt = plt.figtext(0.0, 0.02, cmd_str)
    txt.set_fontfamily('sans-serif')

    plot_path = "{}/{}.png".format(output_path, bench_id)
    plt.savefig(plot_path)
    return Path(plot_path)


def _make_cache_flush_plot(
    cfg, bench_id: str, run_data: t.Dict[Target, BenchList]) \
        -> Path:
    plt.clf()
    output_path = cfg.results_dir / 'plots'

    # The number of runs we did dictates how many IBD charts we should
    # generate.
    num_runs = len(list(run_data.values())[0])

    f, axis_pairs = plt.subplots(num_runs, 1)

    if num_runs == 1:
        axis_pairs = [axis_pairs]

    f.set_size_inches(8, 8)

    total_time_data = []
    peak_mem_data = []

    targets = [target for target in run_data.keys()]
    target_names = [target.name for target in targets]

    # Get the plot title from the first bench command we see.
    cmd_str: str = ''
    title: str = ''

    targets_to_flush_list = [{} for _ in range(num_runs)]

    # Benchlist per target in canonical order (due to sorted-by-default dicts)
    for target, benchlist in zip(targets, run_data.values()):
        # Ensure the data ordering is what we expect.
        assert run_data[target] == benchlist

        if not cmd_str:
            cmd_str = _format_command(benchlist[0].results.command)

        if not title:
            title = benchlist[0].results.title

        for i, b in enumerate(benchlist):
            targets_to_flush_list[i][target] = b.results.flush_events


    def add_iters(b, add=''):
        return b + add + " (x{})".format(num_runs)

    f.suptitle('Cache flushes during\n' + title,
               fontsize=10, fontfamily='sanserif')

    for i, ax in enumerate(axis_pairs):
        ax.set_title('Flush history (run {})'.format(i))

        for target, data in targets_to_flush_list[i].items():
            indices = [d.relative_time for d in data]
            heights = [d.flushed_count for d in data]
            widths = [d.duration_secs for d in data]
            print(indices)
            print(heights)
            print(widths)
            ax.bar(indices, heights, width=widths, label=target.name, alpha=0.5)

        ax.set(ylabel='Coins count')

    plt.tight_layout(rect=[0, 0.1, 1, 0.92])
    txt = plt.figtext(
        0.6, 0.02,
        "Benchmarks performed on\n{}{}".format(
            get_processor_info(),
            _get_git_info([i[0] for i in run_data.values()])))
    txt.set_fontfamily('sans-serif')

    txt = plt.figtext(0.0, 0.02, cmd_str)
    txt.set_fontfamily('sans-serif')

    plot_path = "{}/{}-flush-history.png".format(output_path, bench_id)
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
            _get_git_info([i[0] for i in list(runs.values())[0].values()])))
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
