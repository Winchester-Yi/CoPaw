import { App, Avatar, Button, Card, Space, Tag, Typography } from "antd";
import {
  chatApi,
  type FileManagerCapabilities,
  type FileManagerDirectoryListing,
  type FileManagerItem,
} from "@/api/modules/chat";
import { emitChatWorkspaceFile } from "@/components/agentscope-chat/FileWorkspaceEvents";
import FileManager from "@/pages/Chat/components/FileManager";

const { Paragraph, Text, Title } = Typography;

const demoCapabilities: FileManagerCapabilities = {
  browse: true,
  read: true,
  upload: true,
  edit: true,
  download: true,
  archive: true,
};

const demoItems: Record<string, FileManagerItem[]> = {
  "": [
    {
      name: "项目报告",
      path: "项目报告",
      kind: "directory",
      capabilities: demoCapabilities,
    },
    {
      name: "数据与图表",
      path: "数据与图表",
      kind: "directory",
      capabilities: demoCapabilities,
    },
    {
      name: "README.md",
      path: "README.md",
      kind: "file",
      size_bytes: 1432,
      modified_at: "2026-09-04 10:20",
      capabilities: demoCapabilities,
    },
  ],
  项目报告: [
    {
      name: "2025-Q2",
      path: "项目报告/2025-Q2",
      kind: "directory",
      capabilities: demoCapabilities,
    },
    {
      name: "投资简报.html",
      path: "项目报告/投资简报.html",
      kind: "file",
      size_bytes: 9860,
      modified_at: "2026-09-03 16:45",
      capabilities: demoCapabilities,
    },
    {
      name: "市场研究摘要.md",
      path: "项目报告/市场研究摘要.md",
      kind: "file",
      size_bytes: 2670,
      modified_at: "2026-09-03 14:10",
      capabilities: demoCapabilities,
    },
  ],
  "项目报告/2025-Q2": [
    {
      name: "经营数据.csv",
      path: "项目报告/2025-Q2/经营数据.csv",
      kind: "file",
      size_bytes: 8240,
      modified_at: "2026-09-02 11:30",
      capabilities: demoCapabilities,
    },
    {
      name: "会议纪要.txt",
      path: "项目报告/2025-Q2/会议纪要.txt",
      kind: "file",
      size_bytes: 3920,
      modified_at: "2026-09-01 18:00",
      capabilities: demoCapabilities,
    },
  ],
  数据与图表: [
    {
      name: "区域销量.json",
      path: "数据与图表/区域销量.json",
      kind: "file",
      size_bytes: 1540,
      modified_at: "2026-09-03 09:20",
      capabilities: demoCapabilities,
    },
  ],
};

const demoContents: Record<string, string> = {
  "README.md": "# 项目文件\n\n这里汇集报告、数据和本次会话产物。",
  "项目报告/投资简报.html": `<!doctype html><html><body style="font-family:system-ui;padding:32px;color:#172033"><h1>投资简报</h1><p>2025 年第二季度经营表现稳健，海外业务保持增长。</p><ul><li>营收同比增长 18%</li><li>毛利率提升 2.1 个百分点</li><li>建议持续关注现金流改善</li></ul></body></html>`,
  "项目报告/市场研究摘要.md": "# 市场研究摘要\n\n- 行业需求仍处于恢复阶段\n- 核心产品具备明显竞争优势\n- 建议分批配置，持续跟踪季度数据",
  "项目报告/2025-Q2/经营数据.csv": "月份,收入(万元),毛利率\n4月,3240,31.2%\n5月,3510,32.1%\n6月,3780,33.0%",
  "项目报告/2025-Q2/会议纪要.txt": "会议纪要\n\n1. 确认第二季度经营数据。\n2. 更新投资简报结论。\n3. 下周补充区域销量图表。",
  "数据与图表/区域销量.json": '{\n  "华东": 4820,\n  "华南": 3670,\n  "华北": 2940\n}',
};

const demoFileManagerApi: typeof chatApi.fileManager = {
  listDirectory: async ({ root, path, query }) => {
    const items = demoItems[path] || [];
    const keyword = query?.trim().toLowerCase();
    const filteredItems = keyword
      ? items.filter((item) => item.name.toLowerCase().includes(keyword))
      : items;
    const firstDirectory = items.find((item) => item.kind === "directory");
    const listing: FileManagerDirectoryListing = {
      root,
      path,
      items: filteredItems,
      next_cursor: null,
      has_child_directory: Boolean(firstDirectory),
      first_child_directory: firstDirectory?.path || null,
      capabilities: demoCapabilities,
    };
    return listing;
  },
  readFile: async ({ path }) => ({
    path,
    size_bytes: demoContents[path]?.length || 0,
    is_text: true,
    content: demoContents[path] || "这是一个可编辑的演示文件。",
    is_truncated: false,
    editable: true,
    revision: "demo-revision",
  }),
  saveText: async ({ path, content }) => ({
    path,
    size_bytes: content.length,
    is_text: true,
    content,
    is_truncated: false,
    editable: true,
    revision: "demo-revision",
  }),
  upload: async ({ path }, file) => ({
    name: file.name,
    path: path ? `${path}/${file.name}` : file.name,
    kind: "file",
    size_bytes: file.size,
    modified_at: "刚刚",
    capabilities: demoCapabilities,
  }),
  downloadFile: async ({ path }) => ({
    blob: new Blob([demoContents[path] || "演示文件"], {
      type: "text/plain",
    }),
    filename: path.split("/").pop() || "演示文件.txt",
  }),
  archive: async ({ path }) => ({
    archive_item_id: "demo-archive-item",
    original_path: path,
  }),
  deleteDirectory: async () => undefined,
  restore: async () => ({
    archive_item_id: "demo-archive-item",
    original_path: "项目报告/投资简报.html",
  }),
  purge: async () => ({
    archive_item_id: "demo-archive-item",
    original_path: "项目报告/投资简报.html",
  }),
};

const messages = [
  {
    author: "你",
    content: "请帮我整理这个项目中的报告、数据表和生成文件。",
    tone: "user",
  },
  {
    author: "CoPaw",
    content:
      "我已梳理工作目录。你可以打开右上角的文件管理器，在不离开对话的情况下浏览、上传和编辑文件。",
    tone: "assistant",
  },
  {
    author: "CoPaw",
    content: "文件管理器右侧抽屉已为聊天内容预留空间。",
    tone: "assistant",
  },
];

export default function TestFileManagerDrawerPage() {
  const openSessionFiles = () => {
    [
      {
        fileName: "投资简报示例.html",
        fileUrl:
          "data:text/html;charset=utf-8,%3Ch1%3E%E6%8A%95%E8%B5%84%E7%AE%80%E6%8A%A5%E7%A4%BA%E4%BE%8B%3C%2Fh1%3E%3Cp%3E%E5%BD%93%E5%89%8D%E4%BC%9A%E8%AF%9D%E6%96%87%E4%BB%B6%E5%9C%A8%E5%90%8C%E4%B8%80%E5%B7%A5%E4%BD%9C%E5%8F%B0%E5%86%85%E9%A2%84%E8%A7%88%E3%80%82%3C%2Fp%3E",
      },
      {
        fileName: "会议纪要示例.md",
        fileUrl:
          "data:text/markdown;charset=utf-8,%23%20%E4%BC%9A%E8%AE%AE%E7%BA%AA%E8%A6%81%0A%0A-%20%E6%9B%B4%E6%96%B0%E7%BB%8F%E8%90%A5%E6%95%B0%E6%8D%AE%0A-%20%E8%A1%A5%E5%85%85%E5%8C%BA%E5%9F%9F%E9%94%80%E9%87%8F%E5%9B%BE%E8%A1%A8",
      },
      {
        fileName: "经营数据示例.csv",
        fileUrl:
          "data:text/plain;charset=utf-8,%E6%9C%88%E4%BB%BD%2C%E6%94%B6%E5%85%A5%0A4%E6%9C%88%2C3240%0A5%E6%9C%88%2C3510%0A6%E6%9C%88%2C3780",
      },
    ].forEach((file, index) =>
      emitChatWorkspaceFile({
        action: index === 0 ? "open" : "register",
        ...file,
        enableClickTracking: false,
      }),
    );
  };

  return (
    <App>
      <div
        data-chat-shell
        style={{
          height: "calc(100vh - 32px)",
          minHeight: 640,
          overflow: "hidden",
          background: "#f7f9fc",
        }}
      >
        <section
          className="swe-chat-anywhere-layout"
          data-chat-messages-area
          style={{
            display: "flex",
            minWidth: 0,
            flexDirection: "column",
            background: "#fff",
          }}
        >
          <header
            style={{
              display: "flex",
              minHeight: 60,
              alignItems: "center",
              gap: 12,
              padding: "0 20px",
              borderBottom: "1px solid #e5e7eb",
            }}
          >
            <Avatar style={{ background: "#3769fc" }}>C</Avatar>
            <div style={{ minWidth: 0, flex: 1 }}>
              <Title level={5} style={{ margin: 0 }}>
                文件管理器 Drawer 演示
              </Title>
              <Text type="secondary">
                右上角管理文件，也可从消息文件直接进入预览
              </Text>
            </div>
            <FileManager fileManagerApi={demoFileManagerApi} />
          </header>

          <main
            style={{
              width: "min(760px, 100%)",
              margin: "0 auto",
              padding: "28px 24px",
              overflowY: "auto",
            }}
          >
            <Card
              size="small"
              style={{ marginBottom: 24, borderColor: "#dce6ff" }}
            >
              <Space direction="vertical" size={4}>
                <Text strong>交互检查点</Text>
                <Text type="secondary">
                  打开文件管理器后，当前聊天内容应仍保留在左侧；窗口宽度小于
                  1280px 时，抽屉会自动切换为全宽。
                </Text>
              </Space>
            </Card>

            <Space direction="vertical" size={20} style={{ width: "100%" }}>
              {messages.map((message) => (
                <div
                  key={message.content}
                  style={{
                    display: "flex",
                    justifyContent:
                      message.tone === "user" ? "flex-end" : "flex-start",
                  }}
                >
                  <div
                    style={{
                      maxWidth: "82%",
                      padding: "12px 14px",
                      borderRadius: 10,
                      color: message.tone === "user" ? "#fff" : "#1f2937",
                      background:
                        message.tone === "user" ? "#3769fc" : "#f3f6fb",
                    }}
                  >
                    <Text
                      strong
                      style={{
                        color: message.tone === "user" ? "#fff" : "#111827",
                      }}
                    >
                      {message.author}
                    </Text>
                    <Paragraph
                      style={{
                        margin: "6px 0 0",
                        color: message.tone === "user" ? "#fff" : "#374151",
                      }}
                    >
                      {message.content}
                    </Paragraph>
                  </div>
                </div>
              ))}
            </Space>

            <Tag color="blue" style={{ marginTop: 28 }}>
              演示内容
            </Tag>
            <Button style={{ marginLeft: 12 }} onClick={openSessionFiles}>
              打开会话文件示例
            </Button>
          </main>
        </section>
      </div>
    </App>
  );
}
