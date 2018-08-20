from . import output

from io import StringIO
from textwrap import dedent


def test_print_comparative_times_table():
    # Looped because we had non-deterministic failures due to key ordering.
    for _ in range(30):
        strio = StringIO()
        output.print_comparative_times_table(
            {'a': {'bench1': [1, 1, 1]}, 'b': {'bench1': [3, 3, 3]}},
            strio)

        a = strio.getvalue()
        assert a == dedent(
            """
            # a vs. b (absolute)
            |  name  | iterations |  a  |  b  |
            |--------|-----------:|----:|----:|
            | bench1 |          3 |   1 |   3 |


            # a vs. b (relative)
            |  name  | iterations |  a  |  b  |
            |--------|-----------:|----:|----:|
            | bench1 |          3 |   1 |   3 |

            """
        )


def test_print_times_table():
    # Looped because we had non-deterministic failures due to key ordering.
    for _ in range(30):
        assert output.get_times_table(
            {'a': [1, 2, 3], 'foo': [2.3], 'b.mem-usage': [3000]}) == dedent(
                """
                a: 0:00:01
                a: 0:00:02
                a: 0:00:03
                b.mem-usage: 3.0MiB
                foo: 0:00:02.300000
                """
            )
