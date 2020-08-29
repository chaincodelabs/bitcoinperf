from . import output

from textwrap import dedent


def test_print_comparative_times_table():
    """
    TODO: replace with tests that just deserialize and render actual pickled results.
    """


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
