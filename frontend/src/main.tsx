import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AccessGate } from "./components/AccessGate";
import "./styles.css";
import { publicMode } from "./api";
import { PublicPlatform } from "./public/PublicPlatform";
import "./public/public.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {publicMode ? <PublicPlatform /> : <AccessGate>
      <App />
    </AccessGate>}
  </React.StrictMode>
);
