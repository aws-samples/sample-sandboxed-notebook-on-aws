# Export Fixture

> Fixture notebook for the export tests

*Generated: 2026-11-13 12:00 UTC · 7 cells*

---

# Sales overview

This notebook **summarises** orders.

- [x] load data
- [ ] review

```mermaid
graph LR
  A[S3 bucket] --> B[DuckDB]
  B --> C[Report]
```


---

## PY-1 [3]

*Executed in 1.2 s · peak 182 MB · 63% CPU*

**Parameters:**

- **Region:** us-east-1
- **limit:** 25

```python
import os
# @param {"type": "dropdown", "options": ["us-east-1", "eu-west-1"], "label": "Region"}
region = "us-east-1"
# @param {"type": "slider", "min": 1, "max": 100}
limit = 25
token = os.environ["API_KEY"]  # env ref only
result = compute_something_with_a_really_long_name(arg_0=0, arg_1=1, arg_2=2, arg_3=3, arg_4=4, arg_5=5, arg_6=6, arg_7=7, arg_8=8, arg_9=9, arg_10=10, arg_11=11, arg_12=12, arg_13=13, arg_14=14, arg_15=15, arg_16=16, arg_17=17, arg_18=18, arg_19=19, arg_20=20, arg_21=21, arg_22=22, arg_23=23, arg_24=24, arg_25=25, arg_26=26, arg_27=27, arg_28=28, arg_29=29)
print(f"<b>{region}</b> {limit}")
```

**Output:**

```
<b>us-east-1</b> 25
```

> ✨ *Sets the **region** and the row `limit`.*

---

## SQL-1 [4]

*Executed in 87 ms*

```sql
SELECT region, COUNT(*) AS orders
FROM sales
WHERE region = 'us-east-1' -- filter
GROUP BY region
```

| region | orders |
| --- | --- |
| us-east-1 | 120 |
| eu-west-1 | 80 |


---

## PY-2 [5]

*Executed in 410 ms*

```python
import plotly.express as px
fig = px.bar(x=["a", "b"], y=[1, 2])
fig
```

*(interactive chart — view HTML export)*

---

## PY-3 [6]

```python
import matplotlib.pyplot as plt
plt.plot([1, 2, 3])
plt.show()
```

![Plot](plot-PY-3.png)

---

## PY-4 [7]

*Executed in 5 ms*

```python
raise ValueError("bad <input> value")
```

**Error:**

```
Traceback (most recent call last):
  File "<cell>", line 1, in <module>
ValueError: bad <input> value
```

---

## Notes

See the ```sql``` below:

```sql
SELECT 1
```


---


*Sandboxed Notebook on AWS — Developed by the AWS Startup SA Team*
