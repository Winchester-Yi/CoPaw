import { createContext, useContext, type ReactNode } from "react";

export type FilePreviewPresentation = "modal" | "drawer" | "workspace";

const FilePreviewPresentationContext =
  createContext<FilePreviewPresentation>("modal");

export function FilePreviewPresentationProvider(props: {
  children: ReactNode;
  value: FilePreviewPresentation;
}) {
  return (
    <FilePreviewPresentationContext.Provider value={props.value}>
      {props.children}
    </FilePreviewPresentationContext.Provider>
  );
}

export function useFilePreviewPresentation(): FilePreviewPresentation {
  return useContext(FilePreviewPresentationContext);
}
