import unittest

from hypothesis import given, settings, strategies as st

import forum_state


PROPERTY_SETTINGS = settings(
    max_examples=250,
    deadline=None,
    derandomize=True,
    database=None,
)

COUNT = st.integers(min_value=0, max_value=1_000_000)
DELTA = st.integers(min_value=0, max_value=10_000)
SEAL = st.text(min_size=1, max_size=32)


def make_cursor(timestamp, comments, mentions, seal, marker):
    return {
        "version": 1,
        "timestamp": timestamp,
        "comments": comments,
        "mentions": mentions,
        "seal": seal,
        "marker": marker,
    }


class AckCursorPropertyTests(unittest.TestCase):
    @PROPERTY_SETTINGS
    @given(
        timestamp=COUNT,
        comments=COUNT,
        mentions=COUNT,
        dt=DELTA,
        dc=DELTA,
        dm=DELTA,
        lower_seal=SEAL,
        upper_seal=SEAL,
    )
    def test_comparable_merge_returns_exact_lower_offered_cursor(
        self, timestamp, comments, mentions, dt, dc, dm, lower_seal, upper_seal
    ):
        lower = make_cursor(timestamp, comments, mentions, lower_seal, "lower")
        upper = make_cursor(
            timestamp + dt,
            comments + dc,
            mentions + dm,
            upper_seal,
            "upper",
        )

        self.assertEqual(forum_state.merge_ack_cursor(lower, upper), lower)
        reverse = forum_state.merge_ack_cursor(upper, lower)
        if dt or dc or dm:
            self.assertEqual(reverse, lower)
        else:
            self.assertEqual(reverse, upper)
        self.assertIn(reverse, (lower, upper))

    @PROPERTY_SETTINGS
    @given(
        timestamp=COUNT,
        comments=COUNT,
        mentions=COUNT,
        dt=st.integers(min_value=1, max_value=10_000),
        dc=st.integers(min_value=1, max_value=10_000),
        seal_a=SEAL,
        seal_b=SEAL,
    )
    def test_incomparable_offers_always_fail_closed(
        self, timestamp, comments, mentions, dt, dc, seal_a, seal_b
    ):
        left = make_cursor(timestamp, comments + dc, mentions, seal_a, "left")
        right = make_cursor(timestamp + dt, comments, mentions, seal_b, "right")

        with self.assertRaises(forum_state.StateError):
            forum_state.merge_ack_cursor(left, right)
        with self.assertRaises(forum_state.StateError):
            forum_state.merge_ack_cursor(right, left)

    @PROPERTY_SETTINGS
    @given(
        timestamp=COUNT,
        comments=COUNT,
        mentions=COUNT,
        increments=st.lists(
            st.tuples(DELTA, DELTA, DELTA, SEAL),
            min_size=1,
            max_size=8,
        ),
        first_seal=SEAL,
    )
    def test_monotone_offer_sequence_keeps_first_exact_floor(
        self, timestamp, comments, mentions, increments, first_seal
    ):
        first = make_cursor(timestamp, comments, mentions, first_seal, "first")
        offers = [first]
        current_ts, current_comments, current_mentions = timestamp, comments, mentions
        for index, (dt, dc, dm, seal) in enumerate(increments, start=1):
            current_ts += dt
            current_comments += dc
            current_mentions += dm
            offers.append(
                make_cursor(
                    current_ts,
                    current_comments,
                    current_mentions,
                    seal,
                    f"offer-{index}",
                )
            )

        floor = None
        for offered in offers:
            floor = forum_state.merge_ack_cursor(floor, offered)
        self.assertEqual(floor, first)


if __name__ == "__main__":
    unittest.main()
