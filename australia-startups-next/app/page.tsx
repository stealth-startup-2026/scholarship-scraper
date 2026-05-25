import { sources, startups } from "@/lib/startups";
import { StartupDashboard } from "./StartupDashboard";

const aiCount = startups.filter((startup) => startup.sector.includes("AI")).length;
const cities = Array.from(new Set(startups.map((startup) => startup.city)));

export default function Home() {
  return (
    <main className="shell">
      <section className="masthead" aria-labelledby="page-title">
        <div>
          <p className="eyebrow">Australia startup growth watch</p>
          <h1 id="page-title">Top 10 growing startups in Australia</h1>
          <p className="lede">
            A focused dashboard based on LinkedIn&apos;s 2025 Top Startups ranking,
            which measures employee growth, jobseeker interest, member engagement,
            and the ability to attract talent from major companies.
          </p>
        </div>
        <div className="source-card">
          <span>Data snapshot</span>
          <strong>2025 ranking</strong>
          <p>Curated May 2026 from public startup rankings and reporting.</p>
        </div>
      </section>

      <section className="stats" aria-label="Summary statistics">
        <div>
          <span>{startups.length}</span>
          <p>ranked startups</p>
        </div>
        <div>
          <span>{aiCount}</span>
          <p>AI-led categories</p>
        </div>
        <div>
          <span>{cities.length}</span>
          <p>startup cities</p>
        </div>
        <div>
          <span>4</span>
          <p>growth signals tracked</p>
        </div>
      </section>

      <section className="dashboard" aria-label="Top 10 startups dashboard">
        <div>
          <StartupDashboard startups={startups} />
        </div>

        <aside className="insights" aria-label="Ranking insights">
          <div className="map-card">
            <h2>Where growth is clustering</h2>
            <div className="mini-map" aria-hidden="true">
              <span className="dot sydney">Sydney</span>
              <span className="dot melbourne">Melbourne</span>
              <span className="dot brisbane">Brisbane</span>
            </div>
            <p>
              Sydney and Melbourne dominate the top ten, while Brisbane appears
              through construction procurement software.
            </p>
          </div>

          <div className="theme-card">
            <h2>Sector pattern</h2>
            <p>
              AI and healthtech carry the strongest signal: six of the top ten
              are either AI-first or apply AI directly inside clinical,
              enterprise, or defence workflows.
            </p>
          </div>

          <div className="theme-card">
            <h2>Sources</h2>
            <ul>
              {sources.map((source) => (
                <li key={source.url}>
                  <a href={source.url}>{source.label}</a>
                </li>
              ))}
            </ul>
          </div>
        </aside>
      </section>
    </main>
  );
}
