"""VISUALIZATION — One-liner Plotly charts."""

import pandas as pd
import plotly.express as px


# =============================================================================
# VISUALIZATION — One-liner Plotly charts
# =============================================================================

def _validate_columns(df: pd.DataFrame, func_name: str, **col_args):
    """
    Validate that specified column names exist in the DataFrame.
    Raises a helpful ValueError with 'did you mean?' suggestions if not found.
    
    Args:
        df: The DataFrame to check against
        func_name: Name of the calling function (for error messages)
        **col_args: keyword args mapping param_name → column_name (or None to skip)
    
    Example:
        _validate_columns(df, 'plot_bar', x='revnue', y='quantity')
        → ValueError: plot_bar: column 'revnue' not found. Did you mean 'revenue'?
    """
    from difflib import get_close_matches
    
    available = list(df.columns)
    for param_name, col_name in col_args.items():
        if col_name is None:
            continue
        if col_name not in df.columns:
            # Fuzzy match for suggestions
            suggestions = get_close_matches(col_name, available, n=3, cutoff=0.5)
            msg = f"{func_name}: column '{col_name}' not found in DataFrame"
            if suggestions:
                msg += f". Did you mean: {', '.join(repr(s) for s in suggestions)}?"
            else:
                msg += f". Available columns: {available[:15]}"
                if len(available) > 15:
                    msg += f" ... ({len(available)} total)"
            raise ValueError(msg)


def _apply_dark_theme(fig):
    """Apply dark theme to a Plotly figure to match the notebook's dark UI."""
    fig.update_layout(
        template='plotly_dark',
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
    )
    return fig


def _auto_display(fig):
    """Auto-register a Plotly figure with the display system if available.
    
    This allows plot helpers to render charts even when called inside
    if/else blocks, loops, or functions (where the return value isn't
    captured as the cell's last expression).
    """
    import inspect
    # Walk up the entire call stack to find the executor-injected `display`
    frame = inspect.currentframe()
    try:
        f = frame.f_back  # skip _auto_display itself
        while f:
            display_fn = f.f_locals.get('display') or f.f_globals.get('display')
            if callable(display_fn):
                display_fn(fig)
                break
            f = f.f_back
    except Exception:
        pass
    finally:
        del frame


def plot_line(df: pd.DataFrame, x: str, y: str, color: str = None, title: str = None):
    """
    Interactive line chart.
    
    Example:
        plot_line(df, x='date', y='revenue', color='product', title='Revenue Over Time')
    """
    _validate_columns(df, 'plot_line', x=x, y=y, color=color)
    fig = px.line(df, x=x, y=y, color=color, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_bar(df: pd.DataFrame, x: str, y: str, color: str = None, title: str = None):
    """
    Interactive bar chart.
    
    Example:
        plot_bar(df, x='product', y='revenue', color='region', title='Revenue by Product')
    """
    _validate_columns(df, 'plot_bar', x=x, y=y, color=color)
    fig = px.bar(df, x=x, y=y, color=color, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_scatter(df: pd.DataFrame, x: str, y: str, size: str = None, color: str = None, title: str = None):
    """
    Interactive scatter plot.
    
    Example:
        plot_scatter(df, x='age', y='revenue', size='quantity', color='country')
    """
    _validate_columns(df, 'plot_scatter', x=x, y=y, size=size, color=color)
    fig = px.scatter(df, x=x, y=y, size=size, color=color, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_histogram(df: pd.DataFrame, column: str, bins: int = 30, title: str = None):
    """
    Interactive histogram.
    
    Example:
        plot_histogram(df, 'revenue', bins=20, title='Revenue Distribution')
    """
    _validate_columns(df, 'plot_histogram', column=column)
    fig = px.histogram(df, x=column, nbins=bins, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_heatmap(df: pd.DataFrame, x: str, y: str, value: str, title: str = None):
    """
    Pivot and display as heatmap.
    
    Example:
        plot_heatmap(df, x='month', y='product', value='revenue', title='Sales Heatmap')
    """
    _validate_columns(df, 'plot_heatmap', x=x, y=y, value=value)
    # The heatmap aggregates `value` with sum, so it must be numeric. Catch a
    # non-numeric value column with a clear message instead of pandas' opaque
    # "No numeric types to aggregate" / an all-NaN pivot.
    from pandas.api.types import is_numeric_dtype
    if not is_numeric_dtype(df[value]):
        raise ValueError(
            f"plot_heatmap: value column '{value}' must be numeric to aggregate "
            f"(got dtype {df[value].dtype}). Pass a numeric column for `value`, or "
            f"encode/convert it first (e.g. pd.to_numeric(df['{value}'], errors='coerce'))."
        )
    pivot = df.pivot_table(values=value, index=y, columns=x, aggfunc='sum')
    fig = px.imshow(pivot, title=title, color_continuous_scale='Blues', aspect='auto')
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_box(df: pd.DataFrame, y: str, x: str = None, color: str = None, title: str = None):
    """
    Interactive box plot — distribution, median, quartiles, and outliers of a numeric
    column, optionally split into groups by a categorical column.

    Example:
        plot_box(df, y='revenue')                               # one box
        plot_box(df, y='revenue', x='region', title='Revenue by Region')  # per group
    """
    _validate_columns(df, 'plot_box', y=y, x=x, color=color)
    fig = px.box(df, x=x, y=y, color=color, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_pie(df: pd.DataFrame, names: str, values: str, title: str = None):
    """
    Interactive pie chart — share of a total across categories.

    names: the category column (slice labels). values: the numeric column summed per
    category.

    Example:
        plot_pie(df, names='category', values='revenue', title='Revenue Share')
    """
    _validate_columns(df, 'plot_pie', names=names, values=values)
    from pandas.api.types import is_numeric_dtype
    if not is_numeric_dtype(df[values]):
        raise ValueError(
            f"plot_pie: values column '{values}' must be numeric to sum per slice "
            f"(got dtype {df[values].dtype}). Pass a numeric column for `values`."
        )
    fig = px.pie(df, names=names, values=values, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_area(df: pd.DataFrame, x: str, y: str, color: str = None, title: str = None):
    """
    Interactive area chart — a filled line, ideal for a trend over time or a stacked
    composition when `color` splits the series.

    Example:
        plot_area(df, x='date', y='revenue', title='Revenue Over Time')
        plot_area(df, x='date', y='revenue', color='product')   # stacked by product
    """
    _validate_columns(df, 'plot_area', x=x, y=y, color=color)
    fig = px.area(df, x=x, y=y, color=color, title=title)
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_corr(df: pd.DataFrame, columns: list = None, method: str = "pearson", title: str = None):
    """
    Correlation heatmap of the NUMERIC columns — the one-liner EDA companion to
    profile(). Unlike plot_heatmap (which needs a pre-pivoted x/y/value), this takes a
    raw DataFrame, auto-selects its numeric columns, and renders their correlation
    matrix with a diverging scale (−1 … +1).

    columns: restrict to these columns (default: all numeric columns).
    method:  'pearson' (default), 'spearman', or 'kendall'.

    Example:
        plot_corr(df)
        plot_corr(df, columns=['age', 'income', 'spend'], method='spearman')
    """
    if columns is not None:
        # Validate membership directly (NOT via _validate_columns' **kwargs, which
        # would collide if a column is literally named 'df' or 'func_name').
        missing = [c for c in columns if c not in df.columns]
        if missing:
            from difflib import get_close_matches
            hint = ""
            sugg = get_close_matches(str(missing[0]), list(df.columns), n=3, cutoff=0.5)
            if sugg:
                hint = f" Did you mean: {', '.join(repr(s) for s in sugg)}?"
            raise ValueError(f"plot_corr: column(s) not found: {missing}.{hint}")
        numeric = df[columns].select_dtypes('number')
    else:
        numeric = df.select_dtypes('number')
    if numeric.shape[1] < 2:
        raise ValueError(
            "plot_corr: need at least 2 numeric columns to correlate "
            f"(found {numeric.shape[1]}). Pass `columns=[...]` with numeric columns, "
            "or convert columns to numeric first (e.g. pd.to_numeric(...))."
        )
    corr = numeric.corr(method=method)
    fig = px.imshow(
        corr, title=title, zmin=-1, zmax=1, text_auto='.2f',
        color_continuous_scale='RdBu_r', aspect='auto',
    )
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig


def plot_confusion_matrix(model, X, y, feature_cols=None, labels=None,
                          normalize: bool = False, title: str = None):
    """
    Confusion matrix of a fitted CLASSIFIER on a labeled test set, rendered as a
    labeled heatmap — the visual companion to evaluate_model (accuracy tells you how
    often you're right; this shows WHICH classes get confused).

        plot_confusion_matrix(model, test_df, test_df["label"])
        plot_confusion_matrix(model, X_test, y_test, normalize=True)   # row-% view

    Args:
      model        a fitted estimator/Pipeline (or a SageMakerJob/Endpoint handle).
      X            test features (DataFrame/Series/array); feature_cols narrows a frame.
      y            true labels aligned to X.
      labels       optional explicit class order for the axes (default: sorted unique).
      normalize    True → show per-true-class proportions (rows sum to 1) instead of
                   raw counts; handy when classes are imbalanced.
      title        chart title.

    Only meaningful for classification. Returns the Plotly figure.
    """
    import numpy as np
    from sklearn.metrics import confusion_matrix

    # Resolve a SageMakerJob/Endpoint handle to a live predictor exactly as the ML
    # helpers do, without importing ml at module load (avoids a viz<->ml cycle).
    predictor = model
    if not hasattr(model, "predict") and hasattr(model, "job_name") \
            and hasattr(model, "model_s3_uri"):
        from .ml import _resolve_model
        predictor = _resolve_model(model)

    # Feature matrix (narrow a DataFrame by feature_cols if given).
    if isinstance(X, pd.DataFrame):
        X_eval = X[feature_cols] if feature_cols else X
    else:
        X_eval = X
    y_true = y.to_numpy() if isinstance(y, (pd.Series,)) else np.asarray(y).ravel()
    y_pred = np.asarray(predictor.predict(X_eval)).ravel()

    # A genuine mix of numeric and string labels across y_true/y_pred is a real
    # problem (the model's classes don't line up with the labels) — sklearn's
    # confusion_matrix raises an opaque "Mix of label input types" for it. Detect it
    # here and give an ACTIONABLE message instead: cast BOTH to str to compare them
    # as categories, or fix the label dtype. (We don't silently coerce — a silent
    # str() cast would hide a real train/eval label mismatch.)
    def _is_str_labels(arr):
        return any(isinstance(v, str) for v in arr[:50])
    if _is_str_labels(y_true) != _is_str_labels(y_pred):
        raise ValueError(
            "plot_confusion_matrix: y true/pred label types don't match — one side is "
            "strings and the other is numbers "
            f"(actual sample: {list(np.unique(y_true))[:5]}, predicted sample: "
            f"{list(np.unique(y_pred))[:5]}). The model's output classes must match "
            "your labels. Cast both to the same type (e.g. y.astype(str) and compare "
            "to str predictions), or check you passed the right label column."
        )

    # Union the actual + predicted classes. sort by str() so an all-string or
    # all-numeric set whose numpy union would otherwise choke on ordering is sorted
    # stably regardless of the concrete scalar type.
    classes = labels if labels is not None else sorted(
        set(np.unique(y_true)) | set(np.unique(y_pred)), key=str
    )
    cm = confusion_matrix(y_true, y_pred, labels=classes,
                          normalize='true' if normalize else None)

    text_fmt = '.2f' if normalize else 'd'
    fig = px.imshow(
        cm, x=[str(c) for c in classes], y=[str(c) for c in classes],
        labels=dict(x="Predicted", y="Actual",
                    color="Proportion" if normalize else "Count"),
        title=title or ("Confusion matrix" + (" (row-normalized)" if normalize else "")),
        text_auto=text_fmt, color_continuous_scale='Blues', aspect='auto',
    )
    _apply_dark_theme(fig)
    _auto_display(fig)
    return fig
