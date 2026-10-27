# Goal: speed up `solve(a, lo, hi)` (count pairs with a sum in a range)

Write a Python module that defines `solve(a, lo, hi)`. It must return exactly what this slow reference
returns, for any list `a` of ints and ints `lo`, `hi` (also when `lo > hi`):

```python
def solve(a, lo, hi):
    count = 0
    n = len(a)
    for i in range(n):
        for j in range(i + 1, n):
            s = a[i] + a[j]
            if lo <= s <= hi:
                count += 1
    return count
```

Make it as fast as you can. Speed is measured as CPU time on a list of several thousand ints.
Use only the Python standard library.
