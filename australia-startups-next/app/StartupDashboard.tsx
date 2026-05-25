"use client";

import { useMemo, useState } from "react";
import type { Startup } from "@/lib/startups";

type StartupDashboardProps = {
  startups: Startup[];
};

function getBarWidth(rank: number) {
  return `${108 - rank * 8}%`;
}

export function StartupDashboard({ startups }: StartupDashboardProps) {
  const [query, setQuery] = useState("");
  const [activeSector, setActiveSector] = useState("All");

  const sectors = useMemo(
    () => ["All", ...Array.from(new Set(startups.map((startup) => startup.sector)))],
    [startups],
  );

  const filteredStartups = startups.filter((startup) => {
    const searchText = [
      startup.name,
      startup.city,
      startup.state,
      startup.sector,
      startup.summary,
    ]
      .join(" ")
      .toLowerCase();
    const matchesSearch = searchText.includes(query.toLowerCase().trim());
    const matchesSector =
      activeSector === "All" || startup.sector === activeSector;

    return matchesSearch && matchesSector;
  });

  return (
    <>
      <section className="controls" aria-label="Startup filters">
        <label className="search">
          <span>Search</span>
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Try AI, Melbourne, fintech..."
            value={query}
          />
        </label>
        <div className="chips" aria-label="Sectors represented">
          {sectors.map((sector) => (
            <button
              aria-pressed={activeSector === sector}
              key={sector}
              onClick={() => setActiveSector(sector)}
              type="button"
            >
              {sector}
            </button>
          ))}
        </div>
      </section>

      <div className="result-count" aria-live="polite">
        Showing {filteredStartups.length} of {startups.length} startups
      </div>

      <div className="rank-list">
        {filteredStartups.map((startup) => (
          <article className="startup-row" key={startup.name}>
            <div className="rank" style={{ borderColor: startup.accent }}>
              {startup.rank}
            </div>
            <div className="row-main">
              <div className="row-title">
                <h2>{startup.name}</h2>
                <span>
                  {startup.city}, {startup.state}
                </span>
              </div>
              <p>{startup.summary}</p>
              <div className="bar" aria-hidden="true">
                <span
                  style={{
                    width: getBarWidth(startup.rank),
                    backgroundColor: startup.accent,
                  }}
                />
              </div>
            </div>
            <div className="row-meta">
              <span>{startup.sector}</span>
              <small>Founded {startup.founded}</small>
            </div>
          </article>
        ))}

        {filteredStartups.length === 0 ? (
          <div className="empty-state">
            <h2>No startups match those filters</h2>
            <button
              onClick={() => {
                setActiveSector("All");
                setQuery("");
              }}
              type="button"
            >
              Reset filters
            </button>
          </div>
        ) : null}
      </div>
    </>
  );
}
