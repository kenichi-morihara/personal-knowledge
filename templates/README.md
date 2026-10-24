# 入力テンプレート

このディレクトリは実データではありません。必要な雛形を対象ディレクトリへコピーし、`id: null`を新規UUIDに、updated_atとsourcesを実際の値に置き換えます。`template: true`を削除してください。

- [一般メモ・プロフィール](note.md)
- [計画](project.md)
- [残高スナップショット](balance.md)
- [税金・支払い義務](obligation.md)
- [営業案件](opportunity.md)

UUID生成：`python3 -c 'import uuid; print(uuid.uuid4())'`
