export type Startup = {
  rank: number;
  name: string;
  city: string;
  state: string;
  sector: string;
  summary: string;
  growthSignal: string;
  founded: string;
  accent: string;
};

export const startups: Startup[] = [
  {
    rank: 1,
    name: "Relevance AI",
    city: "Sydney",
    state: "NSW",
    sector: "AI agents",
    summary:
      "Builds AI workforces that automate repeatable business tasks and extract insight from unstructured data.",
    growthSignal: "Ranked #1 on LinkedIn Top Startups Australia 2025.",
    founded: "2020",
    accent: "#0f766e",
  },
  {
    rank: 2,
    name: "Heidi Health",
    city: "Melbourne",
    state: "VIC",
    sector: "Health AI",
    summary:
      "Creates an AI care partner for clinicians, including medical scribing and workflow support.",
    growthSignal:
      "Ranked #2 by LinkedIn and reported as Deloitte Tech Fast 50 winner with 15,323% growth since 2023.",
    founded: "2019",
    accent: "#c2410c",
  },
  {
    rank: 3,
    name: "Howatson+Company",
    city: "Sydney",
    state: "NSW",
    sector: "Creative services",
    summary:
      "Independent creative, media, and advertising company building a durable Australian agency model.",
    growthSignal: "Ranked #3 on LinkedIn Top Startups Australia 2025.",
    founded: "2021",
    accent: "#7c3aed",
  },
  {
    rank: 4,
    name: "Instant",
    city: "Sydney",
    state: "NSW",
    sector: "Commerce fintech",
    summary:
      "Powers ecommerce checkout and retention infrastructure for brands looking to convert more revenue.",
    growthSignal: "Ranked #4 on LinkedIn Top Startups Australia 2025.",
    founded: "2021",
    accent: "#2563eb",
  },
  {
    rank: 5,
    name: "Everlab",
    city: "Melbourne",
    state: "VIC",
    sector: "Preventative health",
    summary:
      "Offers preventative health memberships with diagnostics, clinicians, and personalized interventions.",
    growthSignal: "Ranked #5 on LinkedIn Top Startups Australia 2025.",
    founded: "2023",
    accent: "#16a34a",
  },
  {
    rank: 6,
    name: "V2 AI",
    city: "Sydney",
    state: "NSW",
    sector: "Enterprise AI",
    summary:
      "Data and AI consultancy helping large enterprises modernize operations with applied artificial intelligence.",
    growthSignal: "Ranked #6 on LinkedIn Top Startups Australia 2025.",
    founded: "2023",
    accent: "#0891b2",
  },
  {
    rank: 7,
    name: "Lyrebird Health",
    city: "Melbourne",
    state: "VIC",
    sector: "Health AI",
    summary:
      "AI medical scribe that listens during consultations and drafts clinical notes and documents.",
    growthSignal: "Ranked #7 on LinkedIn Top Startups Australia 2025.",
    founded: "2023",
    accent: "#db2777",
  },
  {
    rank: 8,
    name: "ProcurePro",
    city: "Brisbane",
    state: "QLD",
    sector: "Construction software",
    summary:
      "Digitizes construction procurement so commercial teams can manage contracts, risk, and spend.",
    growthSignal: "Ranked #8 on LinkedIn Top Startups Australia 2025.",
    founded: "2020",
    accent: "#ca8a04",
  },
  {
    rank: 9,
    name: "Arkeus",
    city: "Port Melbourne",
    state: "VIC",
    sector: "Defence technology",
    summary:
      "Builds autonomous optical systems that help machines see, understand, and respond in complex environments.",
    growthSignal: "Ranked #9 on LinkedIn Top Startups Australia 2025.",
    founded: "2021",
    accent: "#475569",
  },
  {
    rank: 10,
    name: "Zeller",
    city: "Melbourne",
    state: "VIC",
    sector: "Business fintech",
    summary:
      "Provides integrated payments, finance management, and business banking tools for Australian companies.",
    growthSignal: "Ranked #10 on LinkedIn Top Startups Australia 2025.",
    founded: "2020",
    accent: "#059669",
  },
];

export const sources = [
  {
    label: "LinkedIn Top Startups Australia 2025",
    url: "https://www.linkedin.com/hubs/top-startups/au/",
  },
  {
    label: "Mediaweek coverage of LinkedIn's 2025 top 10",
    url: "https://www.mediaweek.com.au/ai-and-healthtech-drive-linkedins-2025-top-startups-australia-list/",
  },
  {
    label: "Azzet coverage of Deloitte Tech Fast 50 2025",
    url: "https://azzet.com/news/deloitte-crowns-2025-tech-fast-50-winners",
  },
];
