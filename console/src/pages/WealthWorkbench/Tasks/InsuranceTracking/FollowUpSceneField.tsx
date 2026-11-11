import { useEffect, useState } from "react";
import { Form } from "antd";
import { fetchSceneSkills } from "../../api";
import workbenchStyles from "../../index.module.less";
import { TrackingSelect } from "./TrackingSelect";
import styles from "./Editor.module.less";

export interface FollowUpScene {
  skillId: string;
  sceneName: string;
}

export function FollowUpSceneField({
  defaultScene,
}: {
  defaultScene: FollowUpScene;
}) {
  const form = Form.useFormInstance();
  const [scenes, setScenes] = useState<FollowUpScene[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const selected: FollowUpScene | undefined = Form.useWatch("scene");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(false);
    void fetchSceneSkills("insurance", controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) {
          const available = items
            .filter((item) => item.category === "insurance")
            .map((item) => ({
              skillId: String(item.skillId),
              sceneName: item.senceName || String(item.skillId),
            }));
          setScenes(available);
          const current: FollowUpScene | undefined =
            form.getFieldValue("scene");
          const matched = available.find(
            (scene) => scene.skillId === current?.skillId,
          );
          // 仅同步当前选项名称，不用迟到的请求覆盖用户已选择的 ID。
          if (matched) form.setFieldValue("scene", matched);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [retry, form]);

  const options = new Map(scenes.map((scene) => [scene.skillId, scene]));
  // 默认技能可能已下架；当前选择也不能因重试返回不同列表而丢失。
  for (const scene of [defaultScene, selected]) {
    if (scene?.skillId && !options.has(scene.skillId))
      options.set(scene.skillId, scene);
  }
  return (
    <>
      <Form.Item
        className={styles.sceneField}
        layout="horizontal"
        labelCol={{ flex: "none" }}
        wrapperCol={{ flex: "auto", style: { minWidth: 0 } }}
        name="scene"
        label="经营场景"
        getValueProps={(value?: FollowUpScene) => ({
          value: value?.skillId ?? "",
        })}
        getValueFromEvent={(value: string) => ({
          skillId: value,
          sceneName: options.get(value)?.sceneName ?? value,
        })}
        rules={[
          {
            validator: (_, value?: FollowUpScene) =>
              value?.skillId
                ? Promise.resolve()
                : Promise.reject(new Error("请选择经营场景")),
          },
        ]}
      >
        <TrackingSelect<string>
          aria-required="true"
          loading={loading}
          placeholder="请选择经营场景"
          options={[...options.values()].map((scene) => ({
            value: scene.skillId,
            label: scene.sceneName || scene.skillId,
          }))}
        />
      </Form.Item>
      {!loading && error ? (
        <div className={styles.sceneFeedback} role="alert">
          经营场景加载失败，
          <button
            type="button"
            className={workbenchStyles.link}
            onClick={() => setRetry((value) => value + 1)}
          >
            重新加载
          </button>
        </div>
      ) : null}
    </>
  );
}
