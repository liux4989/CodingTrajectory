import { Card, CardContent } from "./ui/card";

type StateBlockProps = {
  title: string;
  detail?: string;
};

export function StateBlock({ title, detail }: StateBlockProps) {
  return (
    <Card className="state-block">
      <CardContent>
        <p className="state-title">{title}</p>
        {detail ? <p className="state-detail">{detail}</p> : null}
      </CardContent>
    </Card>
  );
}
