# Goal: a bin-packing heuristic that uses as few bins as possible

Write a Python module that defines `pack(items, capacity)`.

- `items` is a list of positive ints (item sizes). `capacity` is a positive int. Every item fits in one bin.
- Return a list of bins. Each bin is a list of item INDEXES (positions in `items`).
- Every index must be in exactly one bin. The sum of the sizes in a bin must be <= `capacity`.
- No bin may be empty.

Fewer bins is better. The score is the mean of ceil(sum(items) / capacity) / number_of_bins over a set of
instances with a few hundred items each. All instances together must finish within a few seconds.
First-fit decreasing is a known starting point; it does not reach the target. Use only the Python standard library.
