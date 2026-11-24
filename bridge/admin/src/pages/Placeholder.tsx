import { PencilRuler } from "lucide-react";
import { Card, PageHead } from "../components/ui";

export function Placeholder({ title, phase, onMenu }: { title: string; phase?: string; onMenu: () => void }) {
  return (
    <div className="page">
      <PageHead title={title} onMenu={onMenu} />
      <Card>
        <div className="placeholder">
          <PencilRuler size={26} />
          <div>这一页还在做</div>
          <div className="placeholder__sub">排在 {phase ?? "后面"} 期，见 docs/admin.md「分期」</div>
        </div>
      </Card>
    </div>
  );
}
