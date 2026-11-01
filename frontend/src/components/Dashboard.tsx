"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { apiGet } from "@/lib/api.mjs";
import {
  ComparisonView,
  EfficiencyView,
  ExperimentsView,
  FinalEvaluationView,
  InterpretabilityView,
  OverviewView,
  PlaygroundView,
  RobustnessView,
} from "./dashboard-views";
import { asObject, asRows, asText, type Data } from "./dashboard-primitives";

const NAVIGATION = [
  ["/", "Overview", "◫"],
  ["/comparison", "Model comparison", "▥"],
  ["/experiments", "Experiments", "⌁"],
  ["/final-evaluation", "Final evaluation", "◉"],
  ["/interpretability", "Interpretability", "⌘"],
  ["/robustness", "Robustness", "◇"],
  ["/efficiency", "Efficiency", "↗"],
  ["/playground", "Prediction playground", "✳"],
];

const PAGES: Record<string, { title: string; subtitle: string; endpoint: string }> = {
  "/": { title: "Overview", subtitle: "IMDb sentiment research, in one place.", endpoint: "/api/overview" },
  "/comparison": { title: "Model comparison", subtitle: "Explore saved quality tracks one dataset and evaluation at a time.", endpoint: "/api/quality" },
  "/experiments": { title: "Experiments", subtitle: "A traceable history of saved model work.", endpoint: "/api/experiments" },
  "/final-evaluation": { title: "Final evaluation", subtitle: "Saved official test results from the historical Phase 13 evaluation.", endpoint: "/api/final-evaluation" },
  "/interpretability": { title: "Interpretability", subtitle: "Inspect saved validation analyses and model activations.", endpoint: "/api/interpretability" },
  "/robustness": { title: "Robustness", subtitle: "Review perturbation, information-loss and semantic challenge results.", endpoint: "/api/robustness" },
  "/efficiency": { title: "Efficiency", subtitle: "The latest active-generation latency, throughput and memory audit.", endpoint: "/api/efficiency" },
  "/playground": { title: "Prediction playground", subtitle: "Run a review through one frozen model or all five.", endpoint: "/api/models" },
};

function PageBody({ route, data }: { route: string; data: Data }) {
  if (route === "/") return <OverviewView data={data} />;
  if (route === "/comparison") return data.available === false
    ? <Unavailable message={asText(data.issue)} />
    : <ComparisonView data={data} />;
  if (route === "/experiments") return <ExperimentsView data={data} />;
  if (route === "/final-evaluation") return data.available === false
    ? <Unavailable message={asText(data.issue)} />
    : <FinalEvaluationView data={data} />;
  if (route === "/interpretability") return data.available === false
    ? <Unavailable message={asText(data.issue)} />
    : <InterpretabilityView data={data} />;
  if (route === "/robustness") return data.available === false
    ? <Unavailable message={asText(data.issue)} />
    : <RobustnessView data={data} />;
  if (route === "/efficiency") return data.available === false
    ? <Unavailable message={asText(data.issue)} />
    : <EfficiencyView data={data} />;
  if (route === "/playground" && asRows(data.models).length === 0 && typeof data.issue === "string") {
    return <Unavailable message={data.issue} />;
  }
  return <PlaygroundView models={asRows(data.models)} />;
}

function Unavailable({ message }: { message: string }) {
  return <div className="empty-state unavailable-state">{message}</div>;
}

export default function Dashboard() {
  const pathname = usePathname();
  const route = PAGES[pathname] ? pathname : "/";
  const page = PAGES[route];
  const [refreshKey, setRefreshKey] = useState(0);
  const [loadState, setLoadState] = useState<{ endpoint: string; data?: Data; error?: string }>({ endpoint: "" });
  const loading = loadState.endpoint !== page.endpoint;
  const data = loading ? null : loadState.data ?? null;
  const error = loading ? "" : loadState.error ?? "";

  useEffect(() => {
    let cancelled = false;
    apiGet<Data>(page.endpoint).then((value) => {
      if (!cancelled) setLoadState({ endpoint: page.endpoint, data: value });
    }).catch((issue: unknown) => {
      if (!cancelled) {
        setLoadState({
          endpoint: page.endpoint,
          error: issue instanceof Error ? issue.message : "Could not load saved OpenBench data.",
        });
      }
    });
    return () => { cancelled = true; };
  }, [page.endpoint, refreshKey]);

  function refresh() {
    setLoadState({ endpoint: "" });
    setRefreshKey((key) => key + 1);
  }

  const overview = route === "/" ? asObject(data) : {};
  const activeGeneration = asText(overview.active_generation_id, "Local research workspace");

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link href="/" className="brand">
          <span className="brand-mark">O</span>
          <span><b>OpenBench</b><small>NLP model benchmarking</small></span>
        </Link>
        <nav aria-label="Main navigation">
          {NAVIGATION.map(([href, label, icon]) => (
            <Link key={href} href={href} className={`nav-link ${route === href ? "active" : ""}`} aria-current={route === href ? "page" : undefined}>
              <span aria-hidden="true">{icon}</span>{label}
            </Link>
          ))}
        </nav>
        <div className="sidebar-foot"><span className="status-dot" /> Local workspace<small>{activeGeneration}</small></div>
      </aside>
      <main className="main-area">
        <header className="topbar">
          <div className="crumb">OpenBench <span>/</span> {page.title}</div>
          <div className="top-actions"><span className="local-pill">Local research</span><button className="icon-button" onClick={refresh} aria-label="Refresh saved data">↻</button></div>
        </header>
        <div className="content">
          <div className="page-heading">
            <div><p className="eyebrow">IMDb SENTIMENT BENCHMARK</p><h1>{page.title}</h1><p className="subtitle">{page.subtitle}</p></div>
          </div>
          {loading && <div className="loading-grid" role="status" aria-label="Loading saved data"><i /><i /><i /></div>}
          {error && <div className="error-banner" role="alert">{error}<button onClick={refresh}>Retry</button></div>}
          {!loading && !error && data && <PageBody route={route} data={data} />}
          <footer className="page-footer">OpenBench · local inference and saved research artifacts</footer>
        </div>
      </main>
    </div>
  );
}
