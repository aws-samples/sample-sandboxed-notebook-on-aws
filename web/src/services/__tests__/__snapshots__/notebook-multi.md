# Export Fixture

> Fixture notebook for the export tests

*Generated: 2026-11-13 12:00 UTC · 3 cells*

---

# Multiple outputs

One cell, several outputs.


---

## PY-1 [8]

*Executed in 640 ms*

```python
print("loading orders…")
display(direct)
print("and the upstream view")
display(upstream)
```

**Output:**

```
loading orders…
```

*Table 1 · direct — 2 rows × 2 cols*

| region | orders |
| --- | --- |
| us-east-1 | 120 |
| eu-west-1 | 80 |


```
and the upstream view
```

*Table 2 · upstream — 12 rows × 1 col*

| region |
| --- |
| us-east-1 |
| eu-west-1 |

*Showing first 2 of 12 rows.*


*Table 3 — 3 rows × 3 cols*

| a | b | c |
| --- | --- | --- |
| 1 | 2 | 3 |

*Showing first 1 of 3 rows.*


*Chart 1 · fig1 · Plotly*

*(interactive chart — view HTML export)*

*Chart 2 · Plotly*

*(interactive chart — view HTML export)*

*Image 1*

![Image 1](plot-PY-1-1.png)

*Image 2*

![Image 2](plot-PY-1-2.png)

*Image 3*

![Image 3](plot-PY-1-3.png)

```
done
```

---

## PY-2 [9]

```python
display(direct)
display(upstream)
raise ValueError("boom")
```

**Output:**

```
loading orders…
```

*Table 1 · direct — 2 rows × 2 cols*

| region | orders |
| --- | --- |
| us-east-1 | 120 |
| eu-west-1 | 80 |


```
and the upstream view
```

*Table 2 · upstream — 12 rows × 1 col*

| region |
| --- |
| us-east-1 |
| eu-west-1 |

*Showing first 2 of 12 rows.*


**Error:**

```
ValueError: boom
```

---


*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*
