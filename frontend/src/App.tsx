import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ArtifactPage } from "./pages/ArtifactPage";
import { HealthPage } from "./pages/HealthPage";
import { InboxPage } from "./pages/InboxPage";
import { NewProjectPage } from "./pages/NewProjectPage";
import { NotFound } from "./pages/NotFound";
import { ProjectPage } from "./pages/ProjectPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { QuestionPage } from "./pages/QuestionPage";
import { RulesPage } from "./pages/RulesPage";
import { TracePage } from "./pages/TracePage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<ProjectsPage />} />
        <Route path="p/:slug" element={<ProjectPage />} />
        <Route path="p/:slug/a/*" element={<ArtifactPage />} />
        <Route path="p/:slug/trace" element={<TracePage />} />
        <Route path="p/:slug/q/:id" element={<QuestionPage />} />
        <Route path="inbox" element={<InboxPage />} />
        <Route path="new" element={<NewProjectPage />} />
        <Route path="rules" element={<RulesPage />} />
        <Route path="health" element={<HealthPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
