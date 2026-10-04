"""pce-core linalg against reference results: textbook eigenpairs, A·v = λ·v residuals,
orthonormality, trace, known angles, exact least-squares lines."""
import math

from apex_router.core import linalg

S = 1 / math.sqrt(2)
M4 = [[4.0, 1.0, 2.0, 0.5], [1.0, 3.0, 0.0, 1.0], [2.0, 0.0, 5.0, 1.5], [0.5, 1.0, 1.5, 2.0]]


def _close_vec(a, b, tol=1e-9):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_jacobi_2x2_textbook():
    values, vectors = linalg.jacobi_eigen([[2.0, 1.0], [1.0, 2.0]])
    assert _close_vec(values, [3.0, 1.0])
    assert _close_vec(vectors[0], [S, S]) and _close_vec(vectors[1], [S, -S])


def test_jacobi_diagonal_sorts_descending():
    values, vectors = linalg.jacobi_eigen([[4.0, 0, 0], [0, 1.0, 0], [0, 0, 9.0]])
    assert values == [9.0, 4.0, 1.0]
    assert vectors == [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def test_jacobi_4x4_eigenpairs_orthonormal_and_trace():
    values, vectors = linalg.jacobi_eigen(M4)
    for lam, v in zip(values, vectors):
        av = [linalg.dot(row, v) for row in M4]
        assert max(abs(a - lam * x) for a, x in zip(av, v)) < 1e-9
    for i, vi in enumerate(vectors):
        for j, vj in enumerate(vectors):
            assert abs(linalg.dot(vi, vj) - (1.0 if i == j else 0.0)) < 1e-9
    assert math.isclose(sum(values), 14.0, rel_tol=1e-12)
    assert values == sorted(values, reverse=True)


def test_jacobi_sign_normalised():
    _, vectors = linalg.jacobi_eigen(M4)
    for v in vectors:
        lead = next(x for x in v if abs(x) > 1e-12)
        assert lead > 0


def test_has_gap():
    assert linalg.has_gap([3.0, 1.0], 1)
    assert not linalg.has_gap([2.0, 2.0], 1)
    assert not linalg.has_gap([3.0, 1.0], 0) and not linalg.has_gap([3.0, 1.0], 2)


def test_principal_angle_known_values():
    assert math.isclose(linalg.principal_angle([1, 0], [0, 1]), math.pi / 2)
    assert math.isclose(linalg.principal_angle([1, 0], [1, 1]), math.pi / 4)
    assert linalg.principal_angle([1, 0], [-2, 0]) == 0.0
    assert linalg.principal_angle([0, 0], [1, 0]) == math.pi / 2


def test_solve_exact_line():
    X = [[1.0, x] for x in (1, 2, 3, 4, 5)]
    y = [2 + 3 * x for x in (1, 2, 3, 4, 5)]
    assert _close_vec(linalg.solve_normal_equations(X, y, ridge=0.0), [2.0, 3.0])
    assert _close_vec(linalg.solve_normal_equations(X, y), [2.0, 3.0], tol=1e-4)


def test_solve_collinear_does_not_raise():
    X = [[1.0, 2.0, 4.0], [1.0, 3.0, 6.0], [1.0, 4.0, 8.0]]
    beta = linalg.solve_normal_equations(X, [1.0, 2.0, 3.0])
    assert len(beta) == 3 and all(math.isfinite(b) for b in beta)


def test_residual_trend():
    assert math.isclose(linalg.residual_trend([0, 1, 2, 3], [0, 1, 2, 3]), 1.0)
    assert linalg.residual_trend([0, 1, 2], [5, 5, 5]) == 0.0
    assert linalg.residual_trend([0], [1]) == 0.0


def test_readout_uses_the_moved_helpers():
    from apex_router.proxy_engine.tuner import readout
    assert readout._solve_normal_equations is linalg.solve_normal_equations
    assert readout._polyfit_slope is linalg.residual_trend
