from . import output

from io import StringIO
from textwrap import dedent


def test_print_comparative_times_table():
    strio = StringIO()

    output.print_comparative_times_table(
        {'a': {'bench1': [1, 2, 3]}, 'b': {'bench1': [2, 3, 3]}},
        strio)

    assert strio.getvalue() == dedent(
        """
        # a vs. b (absolute)
        |  name  | iterations |  a  |   b   |
        |--------|-----------:|----:|------:|
        | bench1 |          3 |   2 | 2.667 |


        # a vs. b (relative)
        |  name  | iterations |  a  |   b   |
        |--------|-----------:|----:|------:|
        | bench1 |          3 |   1 | 1.333 |

        """
    )


def test_print_times_table():
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
