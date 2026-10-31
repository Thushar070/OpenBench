"""Streamlit entry point for the OpenBench dashboard."""

from __future__ import annotations

from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from deeptext.dashboard.data import DashboardData, load_dashboard_data
from deeptext.dashboard.scene import build_scene_html

PROJECT_ROOT = Path(__file__).resolve().parents[3]
OUTPUTS_DIRECTORY = PROJECT_ROOT / "outputs"
SCENE_BUNDLE = Path(__file__).parent / "static" / "three_scene.js"
PAGES = (
    ("Dashboard", "⌂"),
    ("Models", "◈"),
    ("Experiments", "▤"),
    ("Model Comparison", "◫"),
    ("Analysis", "⌕"),
    ("Sentiment Playground", "◉"),
    ("Dataset", "▦"),
    ("About", "ⓘ"),
)

st.set_page_config(
    page_title="OpenBench Dashboard",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_data(ttl=60, show_spinner=False)
def _load_dashboard_data() -> DashboardData:
    return load_dashboard_data(OUTPUTS_DIRECTORY)


@st.cache_data(show_spinner=False)
def _cached_scene_html(_modified_ns: int) -> str:
    return build_scene_html(SCENE_BUNDLE)


def _scene_html() -> str:
    modified_ns = SCENE_BUNDLE.stat().st_mtime_ns if SCENE_BUNDLE.exists() else 0
    return _cached_scene_html(modified_ns)


def _navigate(page: str) -> None:
    st.session_state["openbench_page"] = page


def _inject_styles() -> None:
    stylesheet = (Path(__file__).parent / "static" / "dashboard.css").read_text(
        encoding="utf-8"
    )
    st.markdown(
        f"<style>{stylesheet}</style>",
        unsafe_allow_html=True,
    )


def _render_sidebar() -> str:
    with st.sidebar:
        st.markdown(
            '<div class="ob-brand"><div class="ob-mark">OB</div>'
            '<div><div class="ob-brand-name">OpenBench</div>'
            '<div class="ob-brand-sub">NLP Model Benchmarking</div></div></div>',
            unsafe_allow_html=True,
        )
        current_page = st.session_state.get("openbench_page", "Dashboard")
        for page, icon in PAGES:
            st.button(
                f"{icon}  {page}",
                key=f"nav-{page}",
                type="primary" if page == current_page else "secondary",
                width="stretch",
                on_click=_navigate,
                args=(page,),
            )
        st.divider()
        st.caption("IMDb Movie Reviews · Binary Sentiment")
        st.caption("Phases 1–7 complete · Phase 8 in progress")
        return current_page


def _metric_card(label: str, value: str, caption: str, icon: str, color: str) -> str:
    return (
        '<div class="ob-card">'
        f'<div class="ob-card-icon ob-icon-{color}">{icon}</div>'
        f'<div class="ob-card-label">{label}</div>'
        f'<div class="ob-card-value">{value}</div>'
        f'<div class="ob-card-caption">{caption}</div>'
        "</div>"
    )


def _format_percent(value: float | None, digits: int = 2) -> str:
    return f"{value * 100:.{digits}f}%" if value is not None else "Unavailable"


def _header(page: str) -> str:
    title, subtitle = {
        "Dashboard": (
            "Dashboard",
            "Explore model performance, experiments and insights for IMDb "
            "sentiment analysis.",
        ),
        "Models": (
            "Models",
            "Explore saved model configurations and evaluation status.",
        ),
        "Experiments": (
            "Experiments",
            "Browse completed runs and saved experiment records.",
        ),
        "Model Comparison": (
            "Model Comparison",
            "Compare Logistic Regression and TextCNN using recorded results.",
        ),
        "Analysis": (
            "Analysis",
            "Review the error summaries saved by completed evaluations.",
        ),
        "Sentiment Playground": (
            "Sentiment Playground",
            "Try a saved model on a review you provide.",
        ),
        "Dataset": (
            "Dataset",
            "Explore the documented IMDb dataset and split boundaries.",
        ),
        "About": (
            "About OpenBench",
            "A reproducible IMDb sentiment benchmarking project.",
        ),
    }[page]
    heading, search = st.columns([1.6, 1], vertical_alignment="center")
    with heading:
        st.markdown(f"# {title}")
        st.caption(subtitle)
    with search:
        st.text_input(
            "Search models and experiments",
            placeholder="Search models or experiment IDs...",
            key="dashboard_search",
            label_visibility="collapsed",
        )
        st.markdown(
            '<div class="ob-kicker"><span class="ob-dot"></span>'
            "Phase 8 · Dashboard in progress</div>",
            unsafe_allow_html=True,
        )
    return st.session_state.get("dashboard_search", "").strip().casefold()


def _performance_chart(data: DashboardData) -> None:
    if not data.test_results_verified:
        st.markdown(
            '<div class="ob-empty">Verified Phase 7 test results are unavailable. '
            "Validation scores are intentionally not shown as test results.</div>",
            unsafe_allow_html=True,
        )
        return

    rows = []
    for model_key, model_name in (
        ("logistic_regression", "Logistic Regression"),
        ("textcnn", "TextCNN"),
    ):
        for metric_key, metric_label in (("accuracy", "Accuracy"), ("f1", "F1 score")):
            rows.append(
                {
                    "Model": model_name,
                    "Metric": metric_label,
                    "Score": data.test_metrics[model_key][metric_key] * 100,
                }
            )
    frame = pd.DataFrame(rows)
    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5, size=24)
        .encode(
            x=alt.X("Model:N", title=None, axis=alt.Axis(labelAngle=0)),
            xOffset=alt.XOffset("Metric:N"),
            y=alt.Y("Score:Q", title="Score (%)", scale=alt.Scale(domain=[0, 100])),
            color=alt.Color(
                "Metric:N",
                title=None,
                scale=alt.Scale(
                    domain=["Accuracy", "F1 score"], range=["#159B62", "#FF9B45"]
                ),
            ),
            tooltip=[
                alt.Tooltip("Model:N"),
                alt.Tooltip("Metric:N"),
                alt.Tooltip("Score:Q", format=".2f"),
            ],
        )
        .properties(height=225)
    )
    st.altair_chart(chart, width="stretch", theme=None)


def _confusion_chart(matrix: list[list[int]], title: str) -> None:
    cells = []
    for actual_index, actual in enumerate(("Negative", "Positive")):
        for predicted_index, predicted in enumerate(("Negative", "Positive")):
            cells.append(
                {
                    "Actual": actual,
                    "Predicted": predicted,
                    "Reviews": matrix[actual_index][predicted_index],
                    "Outcome": "Correct"
                    if actual_index == predicted_index
                    else "Error",
                }
            )
    frame = pd.DataFrame(cells)
    base = alt.Chart(frame).encode(
        x=alt.X("Predicted:N", title="Predicted", sort=["Negative", "Positive"]),
        y=alt.Y("Actual:N", title="Actual", sort=["Negative", "Positive"]),
        tooltip=["Actual:N", "Predicted:N", "Reviews:Q", "Outcome:N"],
    )
    heatmap = base.mark_rect(cornerRadius=5, stroke="#FFFFFF", strokeWidth=3).encode(
        color=alt.Color(
            "Outcome:N",
            title=None,
            scale=alt.Scale(domain=["Correct", "Error"], range=["#159B62", "#FF9B45"]),
            legend=None,
        )
    )
    labels = base.mark_text(fontSize=16, fontWeight=700, color="#17231D").encode(
        text=alt.Text("Reviews:Q", format=",")
    )
    st.markdown(f'<div class="ob-section-title">{title}</div>', unsafe_allow_html=True)
    st.altair_chart(
        (heatmap + labels).properties(height=200), width="stretch", theme=None
    )


def _render_dashboard(data: DashboardData, query: str) -> None:
    stat_cards = (
        (
            "Trained Models",
            str(len(data.models)),
            "Logistic Regression · TextCNN",
            "▥",
            "green",
        ),
        (
            "Experiments",
            str(data.experiment_count),
            "Saved Phase 3, 5, 6 and 7 runs",
            "⚗",
            "orange",
        ),
        (
            "Best Test Accuracy",
            _format_percent(data.best_test_accuracy),
            "Verified Phase 7 result",
            "♜",
            "green",
        ),
        (
            "Official Test Reviews",
            f"{data.test_sample_count:,}"
            if data.test_sample_count is not None
            else "Unavailable",
            "Phase 7 aligned predictions"
            if data.test_results_verified
            else "No verified final report",
            "▤",
            "coral",
        ),
    )
    for offset in (0, 2):
        for column, card in zip(
            st.columns(2), stat_cards[offset : offset + 2], strict=True
        ):
            with column:
                st.markdown(_metric_card(*card), unsafe_allow_html=True)

    st.write("")
    with st.container(key="hero-card", border=True):
        hero_copy, hero_scene = st.columns(
            [1, 1.2], gap="medium", vertical_alignment="center"
        )
        with hero_copy:
            st.markdown(
                '<div class="ob-kicker"><span class="ob-dot"></span>IMDb · '
                "Sentiment Research</div>"
                '<div class="ob-hero-title">From Movie Reviews<br>to '
                '<span class="ob-hero-accent">Meaningful Insights</span></div>'
                '<div class="ob-muted">Explore, compare, and understand NLP '
                "models through "
                "interactive visualizations and real experiment results.</div>",
                unsafe_allow_html=True,
            )
            st.button(
                "Explore Experiments",
                type="primary",
                width="stretch",
                on_click=_navigate,
                args=("Experiments",),
            )
            st.button(
                "Sentiment Playground",
                width="stretch",
                on_click=_navigate,
                args=("Sentiment Playground",),
            )
        with hero_scene:
            st.iframe(
                _scene_html(),
                height=320,
                alt="Interactive three-dimensional sentiment pipeline",
            )

    st.write("")
    if data.test_results_verified:
        performance_cards = (
            (
                "Logistic Regression · Test accuracy",
                data.test_metrics["logistic_regression"]["accuracy"],
                "green",
            ),
            (
                "TextCNN · Test accuracy",
                data.test_metrics["textcnn"]["accuracy"],
                "coral",
            ),
            (
                "Logistic Regression · Test F1",
                data.test_metrics["logistic_regression"]["f1"],
                "orange",
            ),
            ("TextCNN · Test F1", data.test_metrics["textcnn"]["f1"], "green"),
        )
        for offset in (0, 2):
            for column, card in zip(
                st.columns(2),
                performance_cards[offset : offset + 2],
                strict=True,
            ):
                label, value, color = card
                with column:
                    st.markdown(
                        _metric_card(
                            label,
                            _format_percent(value),
                            "Official IMDb test split · Phase 7",
                            "●",
                            color,
                        ),
                        unsafe_allow_html=True,
                    )
    else:
        st.warning("Phase 7 test results are unavailable or failed integrity checks.")

    st.write("")
    chart_col, actions_col = st.columns([1.2, 0.8], gap="medium")
    with chart_col, st.container(border=True):
        st.markdown(
            '<div class="ob-section-title">Model Performance · Official Test</div>',
            unsafe_allow_html=True,
        )
        _performance_chart(data)
    with actions_col, st.container(border=True):
        st.markdown(
            '<div class="ob-section-title">Quick Overview</div>', unsafe_allow_html=True
        )
        st.caption("IMDb Large Movie Review Dataset")
        st.markdown(
            f'<div class="ob-card-caption">Official test: '
            f"{data.test_sample_count:,} reviews · "
            f"{data.class_distribution.get('negative', 0):,} negative · "
            f"{data.class_distribution.get('positive', 0):,} positive</div>"
            if data.test_sample_count is not None
            else '<div class="ob-empty">Dataset counts are unavailable.</div>',
            unsafe_allow_html=True,
        )
        st.write("")
        st.button(
            "Model Comparison",
            width="stretch",
            on_click=_navigate,
            args=("Model Comparison",),
        )
        st.button(
            "Open Dataset Details",
            width="stretch",
            on_click=_navigate,
            args=("Dataset",),
        )
        st.button(
            "Try a Sentiment Review",
            width="stretch",
            on_click=_navigate,
            args=("Sentiment Playground",),
        )

    confusion_left, confusion_right = st.columns(2, gap="medium")
    with confusion_left, st.container(border=True):
        if data.test_results_verified:
            _confusion_chart(
                data.confusion_matrices["logistic_regression"],
                "Logistic Regression · Confusion Matrix",
            )
        else:
            st.markdown(
                '<div class="ob-section-title">Confusion Matrices</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="ob-empty">No verified test matrices are available.</div>',
                unsafe_allow_html=True,
            )
    with confusion_right, st.container(border=True):
        if data.test_results_verified:
            _confusion_chart(
                data.confusion_matrices["textcnn"], "TextCNN · Confusion Matrix"
            )

    st.write("")
    with st.container(border=True):
        st.markdown(
            '<div class="ob-section-title">Recent Experiments</div>',
            unsafe_allow_html=True,
        )
        rows = [
            row
            for row in data.experiments
            if not query
            or query
            in " ".join((row.name, row.model, row.split, row.run_id)).casefold()
        ]
        if rows:
            table = pd.DataFrame(
                [
                    {
                        "Phase": f"Phase {row.phase}",
                        "Experiment": row.name,
                        "Model": row.model,
                        "Dataset Split": row.split,
                        "Accuracy": _format_percent(row.accuracy),
                        "F1": _format_percent(row.f1),
                        "Run": row.run_id,
                    }
                    for row in rows
                ]
            )
            st.dataframe(table, hide_index=True, width="stretch")
        elif query:
            st.info(f"No saved experiment matches “{query}”.")
        else:
            st.markdown(
                '<div class="ob-empty">No saved experiment summaries were found.</div>',
                unsafe_allow_html=True,
            )
        if data.result_directory:
            st.markdown(
                f'<div class="ob-source">Test metrics source: '
                f"<code>outputs/{data.result_directory}/final_results.json</code></div>",
                unsafe_allow_html=True,
            )

    for issue in data.issues:
        st.caption(f"Data note: {issue}")


def _render_placeholder(page: str) -> None:
    descriptions = {
        "Models": (
            "Saved model cards and configuration details will be added in a later "
            "dashboard step."
        ),
        "Experiments": (
            "The full experiment browser is not implemented yet. The dashboard "
            "summarizes the saved Phase 3–7 runs."
        ),
        "Model Comparison": (
            "Interactive model comparison is not implemented yet. The dashboard "
            "shows the verified Phase 7 test summary."
        ),
        "Analysis": (
            "Feature importance, activations, and expanded error analysis are not "
            "implemented in this dashboard step."
        ),
        "Sentiment Playground": (
            "Interactive review inference is not implemented in this dashboard "
            "step. No model is loaded on this page."
        ),
        "Dataset": (
            "Dataset inspection tools are not implemented here. The official test "
            "set remains reserved from training and tuning."
        ),
        "About": (
            "OpenBench is an educational IMDb sentiment benchmarking project "
            "using TF-IDF + Logistic Regression and a PyTorch TextCNN."
        ),
    }
    with st.container(border=True):
        st.markdown(
            f'<div class="ob-section-title">{page}</div>', unsafe_allow_html=True
        )
        st.write(descriptions[page])
        st.caption(
            "Phase 8 is being delivered incrementally; this page is a clearly "
            "labeled placeholder."
        )
        st.button(
            "Return to Dashboard",
            type="primary",
            on_click=_navigate,
            args=("Dashboard",),
        )


def main() -> None:
    _inject_styles()
    page = _render_sidebar()
    query = _header(page)
    data = _load_dashboard_data()
    if page == "Dashboard":
        _render_dashboard(data, query)
    else:
        _render_placeholder(page)


if __name__ == "__main__":
    main()
