import {buildSync} from "esbuild";
import {mkdtempSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {spawnSync} from "node:child_process";

const directory=mkdtempSync(join(tmpdir(),"sjh-ui-tests-"));
// The compiled artifact lives beside node_modules for normal package resolution.
const output=join(process.cwd(),"node_modules/.cache/sjh-ui-tests.mjs");
try{
  buildSync({entryPoints:["src/optimization.test.jsx"],outfile:output,bundle:true,platform:"node",
    format:"esm",packages:"external",jsx:"automatic",define:{"import.meta.env.VITE_API_BASE_URL":JSON.stringify("http://test")}});
  const result=spawnSync(process.execPath,["--test",output],{stdio:"inherit",env:process.env});
  process.exitCode=result.status??1;
}finally{rmSync(output,{force:true});rmSync(directory,{recursive:true,force:true});}
