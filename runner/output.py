import datetime
import sys
from collections import defaultdict, namedtuple
from pathlib import Path

import pytablewriter
import numpy
import matplotlib
# Force matplotlib to not use any Xwindows backend.
matplotlib.use('Agg')
import matplotlib.pyplot as plt


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


#
# commits_to_benches: {
#   commit1 -> {
#     bench1 -> [1, 2, 3, ...],
#     bench2 -> [1, 2, ...],
#     ...
#   },
#   ...
# }
#

def print_comparative_times_table(commits_to_benches, pfile=sys.stdout):
    print(file=pfile)
    writer = pytablewriter.MarkdownTableWriter()
    items = sorted(commits_to_benches.items())
    commits = [i[0] for i in items]
    vs_str = ("{}" + (" vs. {}" * (len(commits) - 1))).format(*commits)
    writer.table_name = vs_str + " (absolute)"
    writer.header_list = ["bench name", "x", *commits]
    writer.value_matrix = []
    writer.margin = 1
    writer.stream = pfile

    bench_rows = defaultdict(list)

    for commit, benches in items:
        for bench, values in sorted(benches.items()):
            bench_rows[bench].append(BenchVal(bench, values))

    for bench, row in bench_rows.items():
        writer.value_matrix.append(
            [bench, row[0].count,
             *["{0:.4f} (± {1:.4f})".format(r.avg, r.stddev)
               for r in row]])

    writer.write_table()
    print(file=pfile)

    writer.table_name = vs_str + " (relative)"
    writer.value_matrix = []

    for bench, row in bench_rows.items():
        minval = min([r.avg for r in row])
        normrow = [i.avg / minval for i in row]
        writer.value_matrix.append(
            [bench, row[0].count, *normrow])

    writer.write_table()


def make_plots(cfg, commits_to_benches):
    """
    Generate matplotlib output based upon bench results.
    """
    output_path = cfg.workdir / 'plots'
    output_path.mkdir(exist_ok=True)

    items = list(sorted(commits_to_benches.items()))
    commits_sorted = list(sorted(i[0] for i in items))
    benches_sorted = list(sorted(items[0][1].keys()))
    # num_benches = len(items[0][1])
    benches_no_mem = [i for i in benches_sorted if 'mem-usage' not in i]

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

    for bench in benches_no_mem:
        plt.clf()
        plt.rcParams.update({'font.family': 'monospace'})
        mem_bench = "{}.mem-usage".format(bench)

        if mem_bench not in benches_sorted:
            # TODO don't assume we have mem-usage available
            continue

        f, (ax1, ax2) = plt.subplots(1, 2)
        f.set_size_inches(6, 3)

        data = [
            commits_to_benches[commit][bench] for commit in commits_sorted
        ]

        def add_iters(b, add=''):
            return b + add + " (x{})".format(
                len(commits_to_benches[commits_sorted[0]][b]))

        ax1.boxplot(data)
        ax1.set_title(add_iters(bench))
        ax1.set_xticklabels(commits_sorted)
        ax1.set(ylabel='Seconds')

        data = [
            numpy.array(commits_to_benches[commit][mem_bench]) * 0.001024
            for commit in commits_sorted
        ]

        ax2.boxplot(data)
        ax2.set_title('memory usage')
        ax2.set_xticklabels([i[:22] for i in commits_sorted])
        ax2.set(ylabel='MB')

        plot_path = "{}/{}.png".format(output_path, bench)
        plt.tight_layout()
        plt.savefig(plot_path)
        print("Generated plot for {} at {}".format(bench, plot_path))
