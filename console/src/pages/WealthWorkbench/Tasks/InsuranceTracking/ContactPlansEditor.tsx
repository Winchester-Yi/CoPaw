import { Form } from "antd";
import { Trash2 } from "lucide-react";
import { FOLLOW_UP_METHODS } from "./api";
import { TrackingDatePicker } from "./TrackingDatePicker";
import { trackingNow } from "./trackingDate";
import { LimitedTextarea } from "./LimitedTextarea";
import { TEXT_LIMIT_RULE } from "./textLimit";
import workbenchStyles from "../../index.module.less";
import { TrackingSelect } from "./TrackingSelect";
import styles from "./ContactPlansEditor.module.less";

/** Form.List 维护单条计划的稳定 key，增删中间项不会串写其他计划。 */
export function ContactPlansEditor({ disabled }: { disabled: boolean }) {
  return (
    <Form.List name="contactPlans">
      {(fields, { add, remove }) => (
        <section aria-label="接触计划编辑" className={styles.root}>
          <h3>接触计划</h3>
          {fields.map((field, index) => (
            <div
              key={field.key}
              className={styles.plan}
              role="group"
              aria-label={`接触计划${index + 1}`}
            >
              <div className={styles.heading}>
                <strong>计划 {index + 1}</strong>
                <button
                  type="button"
                  className={`${workbenchStyles.link} ${styles.remove}`}
                  disabled={disabled}
                  aria-label={`删除接触计划${index + 1}`}
                  onClick={() => remove(field.name)}
                >
                  <Trash2 size={14} aria-hidden />
                  删除
                </button>
              </div>
              <div className={styles.row}>
                <Form.Item
                  layout="horizontal"
                  className={styles.inlineField}
                  labelCol={{ flex: "none" }}
                  wrapperCol={{ flex: "auto", style: { minWidth: 0 } }}
                  name={[field.name, "date"]}
                  label="接触日期"
                  rules={[{ required: true, message: "请选择接触日期" }]}
                >
                  <TrackingDatePicker
                    disabled={disabled}
                    aria-required="true"
                  />
                </Form.Item>
                <Form.Item
                  layout="horizontal"
                  className={styles.inlineField}
                  labelCol={{ flex: "none" }}
                  wrapperCol={{ flex: "auto", style: { minWidth: 0 } }}
                  name={[field.name, "method"]}
                  label="触达方式"
                  rules={[{ required: true, message: "请选择触达方式" }]}
                >
                  <TrackingSelect
                    disabled={disabled}
                    aria-required="true"
                    placeholder="请选择"
                    options={Object.entries(FOLLOW_UP_METHODS).map(
                      ([value, label]) => ({ value: Number(value), label }),
                    )}
                  />
                </Form.Item>
              </div>
              <Form.Item
                layout="horizontal"
                className={styles.content}
                labelCol={{ flex: "none" }}
                wrapperCol={{ flex: "auto", style: { minWidth: 0 } }}
                name={[field.name, "content"]}
                label="接触安排"
                rules={[
                  {
                    required: true,
                    whitespace: true,
                    message: "请输入接触安排",
                  },
                  TEXT_LIMIT_RULE,
                ]}
              >
                <LimitedTextarea
                  rows={1}
                  autoSize
                  placeholder="填写接触目的与具体安排"
                  aria-required="true"
                />
              </Form.Item>
            </div>
          ))}
          <button
            type="button"
            className={`${workbenchStyles.link} ${styles.add}`}
            disabled={disabled}
            onClick={() => add({ date: trackingNow(), method: 0, content: "" })}
          >
            新增接触计划
          </button>
        </section>
      )}
    </Form.List>
  );
}
