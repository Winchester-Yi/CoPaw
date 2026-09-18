import FilePreviewModal, {
  type FilePreviewModalProps,
} from "../FilePreviewModal";

export type FilePreviewDrawerProps = Omit<
  FilePreviewModalProps,
  "presentation"
>;

export default function FilePreviewDrawer(props: FilePreviewDrawerProps) {
  return (
    <FilePreviewModal {...props} presentation="drawer" enableAnnotations />
  );
}
