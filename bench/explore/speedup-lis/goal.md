# Goal: speed up `solve(a)` (longest strictly increasing subsequence)

Write a Python module that defines `solve(a)`. It must return exactly what this slow reference returns,
for any list `a` of ints (empty list included):

```python
def solve(a):
    n = len(a)
    if n == 0:
        return 0
    best = [1] * n
    for i in range(n):
        for j in range(i):
            if a[j] < a[i] and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
    return max(best)
```

Make it as fast as you can. Speed is measured as executed Python lines on a list of a few hundred ints.
Do not change the input list in a way that changes the answer. Use only the Python standard library.
