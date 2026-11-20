import type { License } from "@/api/types";

import { Badge } from "./ui";

export function LicenseBadge({ license }: { license: License }) {
  return (
    <a href={license.url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
      <Badge tone={license.commercial ? "good" : "warn"} title={license.summary || license.name}>
        {license.commercial ? "Commercial use OK" : "Personal use only"}
      </Badge>
    </a>
  );
}
