interface PaginationProps {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (offset: number) => void;
}

export default function Pagination({ total, limit, offset, onOffsetChange }: PaginationProps) {
  if (total === 0) return null;
  const start = offset + 1;
  const end = Math.min(offset + limit, total);

  return (
    <div className="pagination">
      <button className="btn btn-ghost" disabled={offset === 0} onClick={() => onOffsetChange(Math.max(0, offset - limit))}>
        Previous
      </button>
      <span>
        {start}–{end} of {total}
      </span>
      <button className="btn btn-ghost" disabled={end >= total} onClick={() => onOffsetChange(offset + limit)}>
        Next
      </button>
    </div>
  );
}
