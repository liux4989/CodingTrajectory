import { Card, CardContent } from "@/components/ui/card";

type StateBlockProps = {
  title: string;
  detail?: string;
};

export function StateBlock({ title, detail }: StateBlockProps) {
  return (
    <Card>
      <CardContent className="grid gap-2">
        <p className="m-0 font-display text-lg font-semibold">{title}</p>
        {detail ? <p className="m-0 text-muted-foreground">{detail}</p> : null}
      </CardContent>
    </Card>
  );
}
