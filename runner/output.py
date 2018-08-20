import datetime
import pytablewriter
import sys
from collections import defaultdict, namedtuple


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


def print_comparative_times_table(commits_to_benches, pfile=sys.stdout):
    print(file=pfile)
    writer = pytablewriter.MarkdownTableWriter()
    vs_str = ("{}" + (" vs. {}" * (len(commits_to_benches) - 1))).format(
        *commits_to_benches.keys())
    writer.table_name = vs_str + " (absolute)"
    writer.header_list = ["name", "iterations", *commits_to_benches.keys()]
    writer.value_matrix = []
    writer.margin = 1
    writer.stream = pfile

    bench_rows = defaultdict(list)

    for commit, benches in commits_to_benches.items():
        for bench, values in sorted(benches.items()):
            bench_rows[bench].append(BenchVal(bench, values))

    for bench, row in bench_rows.items():
        writer.value_matrix.append(
            [bench, row[0].count, *[r.avg for r in row]])

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
