import skeleton from "../CustomerListSkeleton.module.less";

export function InsuranceListSkeleton() {
  return (
    <>
      {[1, 2, 3, 4, 5].map((row) => (
        <tr key={row} aria-hidden="true" className={skeleton.row}>
          {Array.from({ length: 9 }, (_, column) => (
            <td key={column}>
              <span
                className={`${skeleton.placeholder} ${
                  column === 3 || column === 4
                    ? skeleton.long
                    : column === 8
                    ? skeleton.actions
                    : ""
                }`}
              />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}
